/** Declared human answers, independent from DOM, network and execution.
 * A choice question also accepts the owner's own answer when no option fits. */
export interface GuidanceQuestion { question:string; options:string[]; descriptions?:string[] }
export function validGuidanceAnswer(mode:string|undefined,options:string[],questions:GuidanceQuestion[]|undefined,response:string):boolean{
  if(typeof response!=='string'||!response.trim()||response.length>32768)return false;
  if(!mode||mode==='choice'||mode==='free_text')return true;
  if(mode!=='batch'||!questions?.length)return false;
  try{
    const answers=JSON.parse(response) as Array<{index:number;answer:string}>;
    return Array.isArray(answers)&&answers.length===questions.length
      &&new Set(answers.map(value=>value?.index)).size===answers.length
      &&answers.every(value=>value&&Number.isInteger(value.index)&&value.index>=0&&value.index<questions.length
        &&typeof value.answer==='string'&&Boolean(value.answer.trim()));
  }catch{return false;}
}

/** The reply quote that binds a chat answer to one ask_human question: the question as the owner reads it,
 * then its id on its own last line, so the model (and the record) knows exactly which question the reply
 * answers. Every answer path (the question card, "answer in conversation") builds its quote here. */
export function guidanceReplyQuote(guidanceId:string,question:string):string{
  const text=question.trim();
  return guidanceId?`${text}\n\n[question ${guidanceId}]`:text;
}
