/** Native form over existing ask_human batch/free-text authority.
 * Questions come from the owned server Turn; only answer_guidance can accept.
 * Draft fields survive reload and an uncertain ACK reuses the command identity.
 */
import {createButton,createField} from '../ui';
import {setGuidanceQuestion,type ChoicePorts,type ChoiceInteraction} from './choices';
import type {form_prompt} from './contract.generated';
import {validGuidanceAnswer,type GuidanceQuestion} from './guidance-values';

/** Select value of the own-answer entry; declared labels are non-empty text, never NUL-led. */
const OWN_ANSWER='\u0000own';

export function renderGuidanceForm(doc:Document,block:form_prompt,ports:ChoicePorts|undefined,renderMarkdown?:(source:string)=>string){
  const text=(key:string,fallback:string)=>ports?.text?.(key,fallback)??fallback;
  const node=doc.createElement('form');node.className='rich-guidance-form';
  const heading=doc.createElement('p'),fields=doc.createElement('fieldset'),status=doc.createElement('p'),fallback=doc.createElement('p');
  fallback.className='rich-guidance-default';fallback.hidden=true;
  status.className='rich-guidance-status';heading.className='rich-guidance-hint';status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const submit=createButton({label:text('rich.submit','Submit answer'),variant:'primary',type:'submit',size:'sm',disabled:true},doc);
  const reload=createButton({label:text('rich.reload','Reload answer'),variant:'ghost',size:'sm'},doc);
  submit.dataset.richControl='form-submit';reload.dataset.richControl='form-reload';
  const toolbar=doc.createElement('div');toolbar.className='ui-toolbar rich-guidance-actions';toolbar.append(submit,reload);
  node.append(heading,fields,fallback,status,toolbar);
  let disposed=false,saving=false,flight:AbortController|undefined,interaction:ChoiceInteraction|undefined,signature='';
  let inputs:Array<HTMLInputElement|HTMLSelectElement>=[],command:{response:string;id:string}|undefined;
  // A choice question's select can switch to its own-answer field; declared options never bound the owner.
  let owns:Array<HTMLInputElement|undefined>=[];
  const answerOf=(index:number)=>inputs[index].value===OWN_ANSWER?owns[index]?.value.trim()??'':inputs[index].value;
  const showOwn=()=>owns.forEach((own,index)=>{if(own)own.hidden=inputs[index].value!==OWN_ANSWER;});
  const response=()=>interaction?.mode==='batch'?JSON.stringify(inputs.map((_input,index)=>({index,answer:answerOf(index)}))):inputs[0]?answerOf(0):'';
  function availability(){
    fields.disabled=saving||interaction?.state!=='pending';
    submit.disabled=Boolean(flight)||fields.disabled||!interaction||!validGuidanceAnswer(interaction.mode,interaction.options,interaction.questions,response());
    reload.disabled=Boolean(flight)||!ports;node.setAttribute('aria-busy',String(Boolean(flight)));
  }
  function apply(value:ChoiceInteraction){
    if(value.interactionId!==block.interactionId||!['free_text','batch'].includes(value.mode??''))throw new Error('Unsupported form');
    const questions:GuidanceQuestion[]=value.mode==='batch'?value.questions??[]:[{question:value.displayQuestion||value.question,options:[]}];
    if(!questions.length||questions.length>8||questions.some(q=>!q.question||q.question.length>32768||!Array.isArray(q.options)))throw new Error('Invalid form');
    interaction=value;heading.textContent=text('rich.surface.formHint','Your answers will be saved to this task. Execution remains subject to its existing checks.');
    const next=JSON.stringify(questions);
    if(next!==signature){
      signature=next;fields.replaceChildren();inputs=[];owns=[];
      questions.forEach((question,index)=>{
        const field=createField({label:question.question,required:true,
          ...(question.options.length?{choices:[{value:'',label:text('rich.surface.choose','Choose…')},...question.options.map(option=>({value:option,label:option})),
            {value:OWN_ANSWER,label:text('rich.surface.other','Other (write your own)…')}]}:{})},doc);
        const input=field.control as HTMLInputElement|HTMLSelectElement;input.required=true;input.dataset.richControl='form-field-'+index;
        if(input.tagName==='INPUT')(input as HTMLInputElement).maxLength=32768;
        const label=field.el.querySelector('label');
        if(label?.firstChild?.nodeType===3&&renderMarkdown){
          const prose=doc.createElement('span');prose.className='rich-guidance-question';
          setGuidanceQuestion(prose,question.question,renderMarkdown);label.firstChild.replaceWith(prose);
        }
        inputs.push(input);fields.append(field.el);
        if(question.options.length){
          const own=doc.createElement('input');own.className='ui-input rich-guidance-own-input';own.maxLength=32768;own.hidden=true;
          own.placeholder=text('rich.ownAnswerPlaceholder','Type your answer…');own.dataset.richControl='form-field-own-'+index;
          own.setAttribute('aria-label',text('rich.ownAnswer','None of these? Write your own answer'));
          field.el.append(own);owns[index]=own;
        }else owns[index]=undefined;
        if(question.descriptions?.some(Boolean)){
          const help=doc.createElement('details'),summary=doc.createElement('summary');summary.textContent=text('rich.surface.optionDetails','Option details');help.append(summary);
          question.options.forEach((option,i)=>{const line=doc.createElement('p');line.textContent=option+': '+(question.descriptions?.[i]??'');help.append(line);});fields.append(help);
        }
      });
    }
    if(value.state==='answered'){
      if(value.mode==='batch'){
        try{const answers=JSON.parse(value.response??'');for(const item of answers)if(inputs[item.index])restore(item.index,item.answer);}catch{/* Historical free-text batch replies stay visible in receipt. */}
      }else inputs[0].value=value.response??'';
    }
    showOwn();
    fallback.hidden=!value.defaultAnswer||value.state!=='pending';
    fallback.textContent=value.defaultAnswer?`${text('rich.defaultAnswer','If unanswered')}: ${value.defaultAnswer}`:'';
    status.textContent=value.state==='answered'?value.note??text('rich.answerSaved','Answer saved')
      :value.state==='stale'?text('rich.answerStale','This question is no longer answerable. Reload to check its current state.'):value.note??text('rich.surface.unsaved','Answers are not saved until you submit.');
  }
  function restore(index:number,answer:string){
    const own=owns[index];
    if(own&&![...(inputs[index] as HTMLSelectElement).options].some(option=>option.value===answer&&answer!==OWN_ANSWER)){
      inputs[index].value=OWN_ANSWER;own.value=answer;
    }else inputs[index].value=answer;
  }
  async function run(write:boolean){
    if(disposed||flight||!ports||(write&&submit.disabled))return;
    const controller=new AbortController();flight=controller;saving=write;
    status.textContent=text(write?'rich.submitting':'rich.loadingChoices',write?'Saving answer…':'Loading question…');availability();
    try{
      const answer=response();
      if(write&&(!command||command.response!==answer))command={response:answer,id:ports.commandId()};
      const value=write?await ports.submit(block.interactionId,answer,command!.id,controller.signal):await ports.load(block.interactionId,controller.signal);
      if(!disposed&&!controller.signal.aborted)apply(value);
    }catch(error){
      if(!disposed&&!controller.signal.aborted){ports.reportError?.(error);status.textContent=text('rich.surface.formError','The answer could not be confirmed. Your draft is preserved; reload or retry.');}
    }finally{if(flight===controller){flight=undefined;saving=false;if(!disposed)availability();}}
  }
  const onSubmit=(event:Event)=>{event.preventDefault();void run(true);};
  const onReload=()=>void run(false);const onInput=(event:Event)=>{
    showOwn();
    const index=inputs.indexOf(event.target as HTMLInputElement);
    if(event.type==='change'&&index>=0&&inputs[index].value===OWN_ANSWER)owns[index]?.focus();
    availability();
  };
  node.addEventListener('submit',onSubmit);fields.addEventListener('input',onInput);fields.addEventListener('change',onInput);reload.addEventListener('click',onReload);
  availability();if(ports)void run(false);else status.textContent=text('rich.surface.noContext','Open this view in its task to answer.');
  return {node,reload:()=>void run(false),dispose(){disposed=true;flight?.abort();node.removeEventListener('submit',onSubmit);fields.removeEventListener('input',onInput);fields.removeEventListener('change',onInput);reload.removeEventListener('click',onReload);}};
}
