# Slide motion: animations and transitions that survive PPTX export

Reference for `lib/slides/motion.py`. The decision record lives in
[`modules/production.md`](modules/production.md) under "Slide motion"; the model-facing
format is `lib/slides/guide/PPTD_CHEATSHEET.md`. This note says what
PresentationML can express, how viewers render it, and which subset Tofu
writes.

## What OOXML can express

PresentationML (ECMA-376 / ISO/IEC 29500-1 Part 1 §19.5) has two independent
motion mechanisms on a slide (`p:sld`). The `CT_Slide` child order is
`cSld, clrMapOvr, transition, timing, extLst`, and Office ignores a
misplaced element. `lib/slides/pptx_qa.py` enforces that order.

| Mechanism | XML | Notes |
|---|---|---|
| Slide transition | `p:transition` with one effect child (`p:fade`, `p:push`, `p:wipe`, `p:cut`, `p:zoom`, …), `spd="fast\|med\|slow"`, `advClick`, `advTm` | ECMA-376 2006 set. Office 2010 effects live in the `p14` namespace (`prism`, `vortex`, `ripple`, `glitter`, `honeycomb`, `flash`, …) and need `mc:AlternateContent` for older readers. |
| Morph | `mc:AlternateContent` → `mc:Choice Requires="p159"` → `p:transition` → `p159:morph option="byObject\|byWord\|byChar"`, plus an `mc:Fallback` holding an ordinary transition | `p159` = `http://schemas.microsoft.com/office/powerpoint/2015/09/main`, an Office extension outside ECMA-376. A reader that does not know `p159` takes the Fallback. |
| Build / animation timeline | `p:timing/p:tnLst/p:par/p:cTn nodeType="tmRoot"` → `p:seq` + `p:cTn nodeType="mainSeq"` → one `p:par` per click (`cond delay="indefinite"`) → effect `p:cTn presetClass="entr\|emph\|exit\|path" presetID=… nodeType="clickEffect\|withEffect\|afterEffect"` with behaviours (`p:set` on `style.visibility`, `p:animEffect filter="fade"`, `p:animScale`, `p:anim`, `p:animMotion`) | `presetID` values are application-defined. PowerPoint's Animation pane and other readers recognise an effect by `(presetClass, presetID)`. |
| Build by paragraph | target `p:spTgt spid=…/p:txEl/p:pRg st=i end=i`, plus `p:bldLst/p:bldP spid=… grpId=… build="p"` | `bldP` (ISO/IEC 29500-1 §19.5.12, parent `bldLst` §19.5.14) ties effects to the shape's build; `build` is `ST_TLParaBuildType` (§19.7.36). Source: [Open XML SDK `BuildParagraph`](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.presentation.buildparagraph), which reproduces the ISO text and the `<p:bldP spid="3" grpId="0" build="p"/>` example. |

### Preset identities

Readers map `(presetClass, presetID)` to a named effect. LibreOffice's import
tables are the most complete public mapping:
[`pptfilterhelpers.cxx`](https://raw.githubusercontent.com/LibreOffice/core/master/oox/source/ppt/pptfilterhelpers.cxx)
(entrance) and
[`commontimenodecontext.cxx`](https://raw.githubusercontent.com/LibreOffice/core/master/oox/source/ppt/commontimenodecontext.cxx)
(emphasis/exit):

| Class | ID | LibreOffice effect | PowerPoint UI name |
|---|---|---|---|
| entr | 1 | `ooo-entrance-appear` | Appear |
| entr | 2 | `ooo-entrance-fly-in` | Fly In |
| entr | 10 | `ooo-entrance-fade-in` | Fade |
| entr | 22 | `ooo-entrance-wipe` | Wipe |
| entr | 23 | `ooo-entrance-zoom` | Zoom |
| emph | 6 | `ooo-emphasis-grow-and-shrink` | Grow/Shrink |
| emph | 10 | `ooo-emphasis-bold-flash` | Bold Flash |
| emph | 26 | `ooo-emphasis-flash-bulb` | Pulse (PowerPoint 2010+ name; inferred, see gaps) |
| emph | 32 | `ooo-emphasis-teeter` | Teeter |
| exit | 10 | `ooo-exit-fade-out` | Fade |

### python-pptx

python-pptx has no API for transitions or timing. Tofu builds the elements
with lxml on each slide's `_element` before `Presentation.save()`. They go
into the slide part, and package QA validates the result. Shape ids come from
`shape.shape_id`, so animation targets always name real `p:cNvPr/@id` values.

## How viewers render it

| Viewer | Evidence | Behaviour |
|---|---|---|
| PowerPoint (Windows/Mac 365, 2019+, web, mobile) | [Use the Morph transition](https://support.microsoft.com/en-us/office/use-the-morph-transition-in-powerpoint-8dd1c7b2-b935-44f5-a74c-741d8d9244ea), [Morph tips and tricks](https://support.microsoft.com/en-us/office/morph-transition-tips-and-tricks-bc7f48ff-f152-4ee8-9081-d3121788024f) | Native for everything above. Morph: create and play in 365 / 2019 / 2021 / web; play-only in 2016 C2R ≥ 1511. PowerPoint 2016 MSI without the April 2016 updates "plays a Fade transition" instead. Objects match automatically or by a shared name starting `!!`; Morph expects a 1:1 mapping; charts cross-fade instead of morphing. `!!` naming needs 365 1903+ / Mac 16.23+ / 2019 1908+. |
| LibreOffice Impress 26.8.2.2 (this host, headless) | Tofu's exported sample converted to ODP and PDF (below) | Builds import as real on-click effects (`ooo-entrance-fade-in` per paragraph, emphasis preset 26 as `ooo-emphasis-flash-bulb`). Fade transitions import as `smil:type="fade"`. LibreOffice's transition importer ([`slidetransition.cxx`](https://raw.githubusercontent.com/LibreOffice/core/master/oox/source/ppt/slidetransition.cxx)) knows the `p14` set but no `morph`, so it takes the `mc:Fallback`. The sample's Morph slide became a 1 s fade. |
| Keynote | not verified | Microsoft compares Morph with Keynote's Magic Move. Whether Keynote's PPTX import keeps builds or maps Morph was not tested here: no macOS host, and web search was unavailable. |
| WPS Presentation | not verified | WPS has a "平滑" (smooth) transition marketed as Morph-like. Its handling of `p159:morph` and of `bldP` builds was not tested: no WPS install. The fade Fallback is the designed safety net. |
| Google Slides | not verified | Not tested. Its import uses its own animation subset. |

Gaps stated plainly:

- The PowerPoint name "Pulse" for emphasis preset 26 is inferred. LibreOffice
  names the same id `flash-bulb`, the PowerPoint 2003 name. No Microsoft
  document seen here lists preset ids.
- No PowerPoint instance was available, so "opens without repair in
  PowerPoint" is unverified. The XML follows the documented shapes above and
  passes Tofu's package QA and the LibreOffice import.

## What good presentations use

- Morph exists so that motion conveys the same object in a new state. That is
  continuity, not decoration (Microsoft tips above: move, resize or recolour
  the same object across two slides).
- Practitioner and learning-science guidance converges on the same small set.
  Reveal one point at a time so the audience reads what the speaker says
  (Mayer's segmenting and signalling principles). Use one emphasis for the
  key takeaway. Use quiet transitions. Spinning, bouncing and flying effects
  split attention. Mayer's work is cited from domain knowledge; no source was
  opened here.
- Tofu therefore writes only Fade entrances, one emphasis, the Fade
  transition and Morph. Each uses PowerPoint's default duration, so a speaker
  can still retime it in the Animation pane.

## Tofu's vocabulary (page `motion`)

```yaml
motion:
  reveal: [points, chart]  # one click per step; a multi-paragraph text box
                           # reveals one paragraph per click
  emphasize: kpi           # one final click pulses the key number
  continues: true          # Morph from the previous page; same elementId = same object
```

| Purpose | Key | OOXML written |
|---|---|---|
| Build an argument step by step | `reveal` | `entr` 10 (Fade), `clickEffect`, `p:set` visibility + `p:animEffect filter="fade"` 500 ms. For a text box with several paragraphs, one click per paragraph (`p:txEl/p:pRg`) and `bldP build="p"`. |
| Land the key number | `emphasize` | `emph` 26 (Pulse), `p:animScale by=105 %` with `autoRev`, 250 ms each way. A `metric` component resolves to its `--value` text. |
| Same object, new state | `continues` | Morph `byObject` in `mc:AlternateContent`, fade Fallback, `spd="slow"`. Shared elementIds are renamed `!!<elementId>` on both slides. |
| (default) | none | `p:fade` transition, `spd="fast"`; no timing. |

## Evidence

`designs/slide-motion/` holds the LibreOffice-rendered pages of the
three-page sample (`libreoffice-pages.png`) and its README with the import
summary. `tests/slides/test_slides_motion.py` pins the generated XML. Its
`test_libreoffice_imports_motion` repeats the LibreOffice import whenever
`soffice` is on `PATH`.
