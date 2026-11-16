"""External ptrace diagnostic. Product parents retain their original children.

Child memory defaults to one exec and one thread. An explicit threaded-child
mode records every thread's pre-exit snapshot and groups shared process peaks.
Root launcher exec chains and root threads are tracked for cleanup only.
Entry: --receipt PATH --timeout SECONDS -- COMMAND. Linux-only benchmark owner;
no timings or production defaults can be admitted by this observation.
"""
import argparse,ctypes,errno,json,math,os,signal,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from benchmarks.process_memory_counters import process_status as status

lib=ctypes.CDLL(None,use_errno=True)
lib.ptrace.argtypes=[ctypes.c_uint,ctypes.c_int,ctypes.c_void_p,ctypes.c_void_p];lib.ptrace.restype=ctypes.c_long
lib.prctl.argtypes=[ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong];lib.prctl.restype=ctypes.c_int
TRACEME,CONT,OPTIONS,MSG=0,7,0x4200,0x4201
TRACE_FORK,TRACE_VFORK,TRACE_CLONE,TRACE_EXEC,TRACE_EXIT,EXIT_KILL=0x2,0x4,0x8,0x10,0x40,0x100000
FLAGS=TRACE_FORK|TRACE_VFORK|TRACE_CLONE|TRACE_EXEC|TRACE_EXIT|EXIT_KILL
SET_PARENT_DEATH_SIGNAL,SET_CHILD_SUBREAPER,GET_CHILD_SUBREAPER=1,36,37
WALL=0x40000000

def trace(request,pid,value=0):
 ctypes.set_errno(0)
 result=lib.ptrace(request,pid,None,ctypes.c_void_p(value))
 if result==-1 and ctypes.get_errno():raise OSError(ctypes.get_errno(),os.strerror(ctypes.get_errno()))
 return result

def message(pid):
 value=ctypes.c_ulong();trace(MSG,pid,ctypes.addressof(value));return value.value

def snapshot(pid, *, allow_threads=False):
 values=status(pid);assert values['Threads']>=1 and 'VmHWM' in values
 if not allow_threads:assert values['Threads']==1
 resident={}
 for line in Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines():
  key,_,value=line.partition(':')
  if key in ('Rss','Pss'):
   n,unit=value.split();assert unit=='kB';resident[key]=int(n)*1024
 assert len(resident)==2
 return dict(status=values,exit_smaps=resident,peak_rss_estimate=max(values['VmHWM'],resident['Rss']))

class ObservationError(RuntimeError):
 def __init__(self,report):
  self.report=report
  super().__init__(report['error'])


def observe(command, *, timeout_seconds=120, allow_threaded_children=False):
 """Run only in a dedicated, single-threaded observer with no existing children."""
 if not __debug__:raise RuntimeError('exit observer requires validation checks')
 if sys.platform!='linux':raise RuntimeError('exit memory observation requires Linux')
 if type(allow_threaded_children) is not bool:raise ValueError('threaded child observation must be explicit boolean')
 if (not command or isinstance(timeout_seconds,bool) or not isinstance(timeout_seconds,(int,float))
     or timeout_seconds<=0 or not math.isfinite(timeout_seconds)):
  raise ValueError('command and finite positive timeout required')
 if len(list(Path('/proc/self/task').iterdir()))!=1:raise RuntimeError('observer must be single-threaded')
 if Path(f'/proc/self/task/{os.getpid()}/children').read_text().strip():raise RuntimeError('observer already owns children')
 if signal.getitimer(signal.ITIMER_REAL)!=(0.0,0.0):raise RuntimeError('observer already owns a real-time timer')
 saved_handlers={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGALRM)}
 prior_subreaper=ctypes.c_int()
 if lib.prctl(GET_CHILD_SUBREAPER,ctypes.addressof(prior_subreaper),0,0,0)!=0 or lib.prctl(SET_CHILD_SUBREAPER,1,0,0,0)!=0:
  raise OSError(ctypes.get_errno(),'cannot establish owned orphan reaping')
 parent=os.getpid()
 try:root=os.fork()
 except BaseException:
  lib.prctl(SET_CHILD_SUBREAPER,prior_subreaper.value,0,0,0)
  raise
 if root==0:
  try:
   if lib.prctl(SET_PARENT_DEATH_SIGNAL,signal.SIGKILL,0,0,0)!=0 or os.getppid()!=parent:os._exit(111)
   os.setsid();trace(TRACEME,0);os.kill(os.getpid(),signal.SIGSTOP);os.execvpe(command[0],command,dict(os.environ))
  except BaseException:os._exit(111)

 live={root};known={};pending=set();initial=set();root_code=None;failure=None;cleanup_joined=False;cleanup_joined_pids=[]
 event_ordinal=0

 def register(pid,creator):
  assert pid not in known,'TID reused'
  row=status(pid)
  start=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
  known[pid]=dict(pid=pid,tgid=row['Tgid'],creator_tid=creator,
      creator_tgid=known[creator]['tgid'] if creator is not None else None,
      start_ticks=start,execs=[],joined=False,born_event=event_ordinal)
  live.add(pid)
  if row['Tgid']!=root and row['Threads']!=1 and not allow_threaded_children:raise RuntimeError('child threads are not qualified')

 def timeout(signum,_frame):
  raise TimeoutError(f'observer interrupted by signal {signum}')

 try:
  for signum in (signal.SIGALRM,signal.SIGTERM):signal.signal(signum,timeout)
  signal.setitimer(signal.ITIMER_REAL,timeout_seconds)
  pid,wait=os.waitpid(root,0)
  if os.WIFEXITED(wait) or os.WIFSIGNALED(wait):live.remove(root)
  assert pid==root and os.WIFSTOPPED(wait) and os.WSTOPSIG(wait)==signal.SIGSTOP
  register(root,None);trace(OPTIONS,root,FLAGS);trace(CONT,root)
  while True:
   try:pid,wait,usage=os.wait4(-1,WALL)
   except ChildProcessError:
    assert not live and not pending
    break
   event_ordinal+=1
   if os.WIFEXITED(wait) or os.WIFSIGNALED(wait):
    assert pid in known
    row=known[pid]
    if row['joined']:
     row['subreaper_waited']=True
    row.update(joined=True,returncode=os.waitstatus_to_exitcode(wait),wait4_peak_rss_bytes=usage.ru_maxrss*1024)
    row.setdefault('joined_event',event_ordinal)
    live.discard(pid)
    if pid==root:root_code=row['returncode']
    continue
   assert os.WIFSTOPPED(wait)
   event=wait>>16;sig=os.WSTOPSIG(wait)
   if pid not in known:
    assert event==0 and sig==signal.SIGSTOP
    pending.add(pid);live.add(pid);continue
   row=known[pid]
   if event in (1,2,3):
    child=message(pid);register(child,pid)
    if child in pending:pending.remove(child);trace(CONT,child)
    else:initial.add(child)
   elif event==4:
    assert message(pid)==pid,'non-leader exec is not qualified'
    if pid!=root and row['execs']:raise RuntimeError('multiple child execs are not qualified')
    row['execs'].append(dict(executable=os.readlink(f'/proc/{pid}/exe'),status=status(pid),event=event_ordinal))
   elif event==6:
    row['exit_event_status']=message(pid)
    row['exit_event']=event_ordinal
    if row['tgid']!=root:
     leader=known[row['tgid']]
     if allow_threaded_children and len(leader['execs'])==1:
      row['memory']=snapshot(pid,allow_threads=True)
     elif len(row['execs'])==1:row['memory']=snapshot(pid)
     else:row['pre_exec_exit']=True
   elif event:
    raise RuntimeError(f'unsupported ptrace event {event}')
   elif pid in initial and sig==signal.SIGSTOP:
    initial.remove(pid);sig=0
   elif sig in (signal.SIGSTOP,signal.SIGTSTP,signal.SIGTTIN,signal.SIGTTOU):
    raise RuntimeError('external group stop is not qualified')
   trace(CONT,pid,0 if event else sig)
  assert root_code is not None and all(r['joined'] for r in known.values())
 except BaseException as error:
  failure=error
 finally:
  signal.setitimer(signal.ITIMER_REAL,0)
  for signum in (signal.SIGINT,signal.SIGTERM,signal.SIGALRM):signal.signal(signum,signal.SIG_IGN)
  try:
   if live or pending:
    while True:
     for pid in list(live|pending):
      try:os.kill(pid,signal.SIGKILL)
      except ProcessLookupError:pass  # The owned tracee exited before kill reached it.
     try:pid,wait=os.waitpid(-1,WALL)
     except ChildProcessError:break
     except InterruptedError:continue
     if os.WIFEXITED(wait) or os.WIFSIGNALED(wait):
      live.discard(pid);pending.discard(pid);cleanup_joined_pids.append(pid)
      if pid in known:known[pid].update(joined=True,returncode=os.waitstatus_to_exitcode(wait),cleanup_waited=True)
      if pid==root:root_code=os.waitstatus_to_exitcode(wait)
      continue
     if os.WIFSTOPPED(wait):
      if wait>>16 in (1,2,3):
       try:live.add(message(pid))
       except OSError as error:
        if error.errno!=errno.ESRCH:raise
      live.add(pid)
      try:trace(CONT,pid,signal.SIGKILL)
      except OSError as error:
       if error.errno!=errno.ESRCH:raise
    assert not live and not pending
  except BaseException as cleanup:
   if failure is None:failure=cleanup
   else:failure.add_note('exit observer cleanup failed: '+type(cleanup).__name__)
  finally:
   cleanup_joined=not live and not pending
   for signum,handler in saved_handlers.items():signal.signal(signum,handler)
   if lib.prctl(SET_CHILD_SUBREAPER,prior_subreaper.value,0,0,0)!=0:
    error=OSError(ctypes.get_errno(),'cannot restore subreaper state')
    if failure is None:failure=error
    else:failure.add_note(str(error))
 report=dict(format='tofu.process-exit-memory/v1',memory_observation_only=True,
     boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
     performance_admission=False,root_pid=root,root_returncode=root_code,
     allow_threaded_children=allow_threaded_children,
     all_tracees_joined=cleanup_joined,tracees=list(known.values()),cleanup_joined_pids=cleanup_joined_pids,
     child_memory_complete=all('memory' in row for row in known.values() if row['tgid']!=root),
     memory_scope=('single-exec child process: maximum of every thread pre-exit VmHWM/RSS estimate; shared threads counted once; PSS is not a historical peak'
                   if allow_threaded_children else 'single-exec single-thread child: kernel VmHWM estimate and stopped-exit smaps RSS/PSS; PSS is not a historical peak'))
 if allow_threaded_children:
  groups={}
  for row in known.values():
   if row['tgid']!=root:groups.setdefault(row['tgid'],[]).append(row)
  report['child_process_groups']=[dict(tgid=tgid,tids=[r['pid'] for r in rows],
      complete=all('memory' in r and r['joined'] for r in rows),
      peak_rss_estimate=max((r['memory']['peak_rss_estimate'] for r in rows if 'memory' in r),default=None))
      for tgid,rows in groups.items()]
 if failure is not None:
  report.update(observation_valid=False,error=type(failure).__name__+': '+str(failure),error_notes=getattr(failure,'__notes__',[]))
  raise ObservationError(report) from failure
 report['observation_valid']=True
 return report


def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--receipt',type=Path,required=True)
 parser.add_argument('--timeout',type=float,default=120)
 parser.add_argument('--allow-threaded-children',action='store_true')
 parser.add_argument('command',nargs=argparse.REMAINDER)
 args=parser.parse_args()
 command=args.command[1:] if args.command[:1]==['--'] else args.command
 if args.receipt.exists():raise FileExistsError(args.receipt)
 try:report=observe(command,timeout_seconds=args.timeout,allow_threaded_children=args.allow_threaded_children)
 except ObservationError as error:report=error.report
 with args.receipt.open('x') as destination:destination.write(json.dumps(report,indent=2)+'\n')
 print(json.dumps({key:report[key] for key in ('observation_valid','root_returncode','all_tracees_joined')}))
 return 0 if report['observation_valid'] and report['root_returncode']==0 else 1


if __name__=='__main__':raise SystemExit(main())
