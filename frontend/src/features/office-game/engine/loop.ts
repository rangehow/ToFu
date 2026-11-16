/** Game loop over visible time. Every animation frame advances the simulation
 * by exactly the visible time since the previous frame, in equal steps of at
 * most STEP_MS, and renders once: each frame shows the world at its own time.
 * (A fixed-step accumulator drops or doubles a step whenever frame times do not
 * divide into steps, as with coarsened rAF timestamps on a 60 Hz display or a
 * 144 Hz one, and the motion judders.) The loop asks the world when it next
 * changes: 0 keeps frames coming, a positive delay sleeps on one timer until
 * that keyframe, Infinity sleeps with no callbacks at all.
 * Hidden pages own nothing. Frame scheduler and timers are injectable.
 * Entry: createGameLoop. No DOM, assets or task state.
 */
/** The longest single simulation step (one 60 Hz frame): a long frame is
 * advanced in several. */
export const STEP_MS = 1000 / 60;
/** Liveness bound: after a stalled or throttled frame the simulation never
 * replays more elapsed time than this in one burst. */
export const MAX_FRAME_MS = 250;

export interface GameLoopPorts {
  step(dtMs:number):void;
  render(nowMs:number):void;
  /** 0 = next frame; >0 = ms until the next scheduled visual change; Infinity = asleep. */
  nextWake(nowMs:number):number;
  visible():boolean;
  frames?:{request(callback:(now:number)=>void):number;cancel(id:number):void};
  timers?:{set(callback:()=>void,ms:number):number;clear(id:number):void};
}

export function createGameLoop(ports:GameLoopPorts) {
  const frames=ports.frames||{request:(callback:(now:number)=>void)=>window.requestAnimationFrame(callback),cancel:(id:number)=>window.cancelAnimationFrame(id)};
  const timers=ports.timers||{set:(callback:()=>void,ms:number)=>window.setTimeout(callback,ms),clear:(id:number)=>window.clearTimeout(id)};
  let frame=0,timer=0,disposed=false;
  // Undefined means a fresh clock: the next frame renders without stepping.
  let last:number|undefined;
  const metrics={frames:0,steps:0,sleeps:0,timers:0};
  function clearTimer(){if(timer){timers.clear(timer);timer=0;}}
  function request(){if(!frame&&!disposed&&ports.visible())frame=frames.request(tick);}
  function settle(now:number){
    clearTimer();
    const wake=ports.nextWake(now);
    if(wake<=0){request();return;}
    // Sleeping worlds resume from a fresh clock: no motion was pending.
    last=undefined;
    if(Number.isFinite(wake)){metrics.timers++;timer=timers.set(()=>{timer=0;request();},wake);}
    else metrics.sleeps++;
  }
  function tick(now:number){
    frame=0;
    if(disposed||!ports.visible()){last=undefined;return;}
    if(last!==undefined){
      const elapsed=Math.min(Math.max(0,now-last),MAX_FRAME_MS);
      const count=Math.ceil(elapsed/STEP_MS);
      for(let index=0;index<count;index++){ports.step(elapsed/count);metrics.steps++;}
    }
    last=now;
    ports.render(now);metrics.frames++;
    settle(now);
  }
  return {
    /** Render on the next frame and keep running while the world is busy. */
    wake(){if(disposed)return;clearTimer();request();},
    /** Release every callback; a later wake() starts from a fresh clock. */
    stop(){clearTimer();if(frame){frames.cancel(frame);frame=0;}last=undefined;},
    pending(){return {frame:!!frame,timer:!!timer};},
    metrics(){return {...metrics};},
    dispose(){if(disposed)return;disposed=true;clearTimer();if(frame){frames.cancel(frame);frame=0;}},
  };
}
