"""Office owner-action classifications and click-only default confirmation in isolated Chromium."""
import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin  # noqa: F401
from tests.support.frontend_visual import frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1180, 390])
def test_settled_cards_and_progress_are_not_owner_actions(frontend_visual_page, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    for name in ['attention', 'model', 'brief', 'panel']:
        page.add_script_tag(path=native_module_path(
            f'.native/owner-attention-{name}.js', ROOT / f'frontend/src/features/world/{name}.ts'))
    page.evaluate("""() => {
      const turns = [{turnId:'ask',ordinal:1,actor:'assistant',createdAt:1,projection:{toolRounds:[
        {toolName:'ask_human',guidanceId:'answered',guidanceQuestion:'Already answered?',
          guidanceBlocking:false,status:'done',guidanceResponse:'Keep queued'},
        {toolName:'ask_human',guidanceId:'superseded',guidanceQuestion:'Old question?',
          guidanceBlocking:false,status:'done',guidanceSuperseded:true},
        {toolName:'ask_human',guidanceId:'open',guidanceQuestion:'Keep queued?',
          guidanceBlocking:false,status:'done',guidanceDefault:'Keep queued'},
      ]}}];
      window.fixture = {turns, confirmations:[], replies:[], input:{key:'/fixture',kind:'project',name:'Fixture',
        color:'#58b7a8',leadId:'lead',team:{conversations:[],nextCursor:null,coverage:'complete'},
        assignments:[],progress:null,questions:[],failed:false,reports:{unread:true,
          latest:{turnId:'progress',ordinal:2,title:'Experiments progressing',content:'Running normally',kind:'progress',createdAt:2}}}};
      const panel = createPanel({close(){},conversation(){},answerPorts:()=>undefined,answerInChat(){},open(){},
        downloadUrl:id=>id,more(){},retry(){},answered:()=>render(),confirmDefault:async decision=>{
          fixture.confirmations.push(decision.guidanceId);
          fixture.turns[0].projection.toolRounds.find(r=>r.guidanceId===decision.guidanceId)
            .guidanceDefaultAccepted={at:3,by:1};
        }});
      document.body.replaceChildren(panel.head,panel.body);
      window.render = () => {
        fixture.input.questions=openQuestions('lead',fixture.turns);
        const office=projectOffice(10,[fixture.input],{router:'Router',watchdog:'Watchdog',lead:()=> 'Fixture lead'});
        const person=office.people.get('lead');
        fixture.emphasis=person.emphasis;
        panel.render(projectBrief(person,fixture.input,10),{team:'Fixture',color:'#58b7a8',
          state:person.emphasis==='decision'?'Waiting for you':'Idle',tone:person.emphasis||'idle'},10);
      };
      render();
    }""")
    card = page.locator('.w-question-card')
    assert card.get_attribute('data-guidance-id') == 'open'
    assert page.locator('.w-question-count').count() == 0, 'settled cards do not enter the owner queue'
    assert page.locator('.w-status').get_attribute('data-tone') == 'decision'
    assert page.evaluate('fixture.confirmations') == [], 'rendering never approves a default'
    assert page.evaluate('fixture.replies') == []
    page.locator('.w-question-confirm').click()
    page.wait_for_function("document.querySelector('.w-question-card')?.dataset.state === 'confirmed'")
    assert page.evaluate('fixture.confirmations') == ['open']
    assert page.evaluate('fixture.replies') == []
    assert page.locator('.w-question-confirm').count() == 0
    assert page.locator('.w-status').get_attribute('data-tone') == 'idle'
    assert page.evaluate('fixture.emphasis') is None
    assert page.locator('[data-section="progress"]').inner_text().find('Experiments progressing') >= 0
