#!/usr/bin/env python3
import argparse,csv,datetime as dt,json,re,sys,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
REQ=['run_manifest.json','source_manifest.json','assumption_register.csv','red_team.md','report.md','forecast_snapshot.json','delivery_quality_rubric.json','forward_signal_cards.csv','historical_query_log.csv','source_independence_map.csv']
SHEETS={'summary':['summary'],'sources':['source'],'history':['history'],'quarterly':['quarter'],'drivers':['driver'],'financials':['financial'],'cash':['cash','capital'],'scenarios':['scenario'],'valuation':['valuation'],'monitoring':['monitor'],'manifest':['manifest']}
REPORT={'cutoff':['information cutoff','cutoff'],'conclusion':['conclusion'],'base':['base forecast'],'drivers':['drivers'],'forward':['forward evidence'],'cash':['cash flow','capital'],'scenarios':['scenario','bear','bull'],'valuation':['valuation'],'reverse':['reverse','implicit'],'monitoring':['monitor'],'limitations':['limitation','human-required']}
def parse(v):
 v=v.strip(); v=v[:-1]+'+00:00' if v.endswith('Z') else v
 x=dt.datetime.fromisoformat(v); return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
def sheets(p):
 with zipfile.ZipFile(p) as z: root=ET.fromstring(z.read('xl/workbook.xml'))
 ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
 return [n.attrib.get('name','').lower() for n in root.findall('.//m:sheets/m:sheet',ns)]
def fail(errors,name,detail): errors.append({'check':name,'detail':detail})
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--workspace',required=True);ap.add_argument('--strict',action='store_true');a=ap.parse_args();w=Path(a.workspace);errors=[];checks=[]
 for n in REQ:
  if not (w/n).exists() or (w/n).stat().st_size==0: fail(errors,'file:'+n,'missing or empty')
 model=w/'model/model.xlsx'
 if not model.exists(): fail(errors,'file:model.xlsx','missing')
 try:
  m=json.loads((w/'run_manifest.json').read_text()); need=['run_id','entity','security','as_of','purpose','fiscal_calendar','currency','accounting_basis','horizons','selected_mechanisms','readiness_target','phase_status']; miss=[k for k in need if not m.get(k)]
  if miss: fail(errors,'manifest-fields',','.join(miss))
  inc=[k for k,v in m.get('phase_status',{}).items() if str(v).lower() not in {'complete','completed','done','pass','passed'}]
  if inc: fail(errors,'manifest-phases',','.join(inc))
 except Exception as e: fail(errors,'manifest-json',str(e));m={}
 try:
  sm=json.loads((w/'source_manifest.json').read_text()); src=sm.get('sources',[]); official=[s for s in src if s.get('evidence_tier') in {'E0','E1'}]
  if len(official)<6: fail(errors,'official-source-count',str(len(official)))
  types={str(s.get('source_type','')).lower() for s in official}
  if not any('filing' in t or t in {'10-k','10-q','20-f','annual-report'} for t in types):fail(errors,'official-filing','none')
  if not any('earning' in t or 'results' in t or 'dialogue' in t for t in types):fail(errors,'official-earnings','none')
  cutoff=parse(sm.get('as_of') or m.get('as_of'))
  fields=['source_id','source_type','publisher','published_at','retrieved_at','period_scope','evidence_tier','location','claim_or_fact','allowed_use']
  for s in src:
   miss=[k for k in fields if not s.get(k)]
   if miss: fail(errors,'source-fields',s.get('source_id','?')+':'+','.join(miss))
   try:
    if parse(s['published_at'])>cutoff:fail(errors,'source-cutoff',s.get('source_id','?'))
   except Exception as e:fail(errors,'source-date',str(e))
 except Exception as e: fail(errors,'source-json',str(e)); cutoff=parse(m.get('as_of','2018-02-26T23:59:59Z'))
 try:
  with (w/'forward_signal_cards.csv').open(encoding='utf-8-sig',newline='') as f:sig=list(csv.DictReader(f))
  clusters={s.get('independence_cluster','') for s in sig if s.get('independence_cluster')};families={s.get('source_family','').lower() for s in sig if s.get('source_family')}
  if len(sig)<3:fail(errors,'forward-signal-count',str(len(sig)))
  if len(clusters)<2:fail(errors,'forward-clusters',str(len(clusters)))
  if len(families)<2:fail(errors,'forward-families',str(len(families)))
  base=[s for s in sig if s.get('allowed_use','').lower() in {'base_point','base_driver'}]; bcl={s.get('independence_cluster','') for s in base}; direct={'official-dialogue','cross-company-official','industry-research','official-product','measurement','regulatory','official-transaction'}
  if base and (len(bcl)<2 or not any(s.get('source_family','').lower() in direct for s in base)):fail(errors,'base-permission','failed')
  for s in sig:
   try:
    if parse(s.get('published_at',''))>cutoff:fail(errors,'future-signal',s.get('signal_id','?'))
   except Exception as e:fail(errors,'signal-date',str(e))
   fam=s.get('source_family','').lower();allowed=s.get('allowed_use','').lower();tier=s.get('evidence_tier','').upper()
   if any(x in fam for x in ['technical','paper','standard']) and allowed in {'base_point','base_driver'}:fail(errors,'technical-to-base',s.get('signal_id','?'))
   if tier=='E4' and allowed not in {'monitor','monitor_trigger'}:fail(errors,'E4-permission',s.get('signal_id','?'))
  with (w/'historical_query_log.csv').open(encoding='utf-8-sig',newline='') as f:q=list(csv.DictReader(f))
  if len(q)<3:fail(errors,'query-count',str(len(q)))
  for r in q:
   if str(r.get('future_outcome_terms_used','')).lower() not in {'','false','0','no','none'}:fail(errors,'query-contamination',r.get('query_id','?'))
   if parse(r.get('cutoff',''))!=cutoff:fail(errors,'query-cutoff',r.get('query_id','?'))
  with (w/'source_independence_map.csv').open(encoding='utf-8-sig',newline='') as f:im=list(csv.DictReader(f))
  mapped={r.get('cluster_id','') for r in im}; missing=clusters-mapped
  if missing:fail(errors,'unmapped-clusters',','.join(sorted(missing)))
 except Exception as e:fail(errors,'forward-evidence',str(e))
 try:
  with (w/'assumption_register.csv').open(encoding='utf-8-sig',newline='') as f:r=csv.DictReader(f);fields=set(r.fieldnames or []);rows=list(r)
  need={'assumption_id','entity','segment','mechanism','metric','period','scenario','value','unit','evidence_tier','source_ids','confidence','breakpoint','next_evidence','owner'}
  if need-fields:fail(errors,'assumption-headers',','.join(sorted(need-fields)))
  if not rows:fail(errors,'assumption-rows','none')
 except Exception as e:fail(errors,'assumption-csv',str(e))
 try:
  text=(w/'report.md').read_text().lower()
  for k,aliases in REPORT.items():
   if not any(x in text for x in aliases):fail(errors,'report:'+k,'missing')
  if len(text)<5000:fail(errors,'report-depth',str(len(text)))
 except Exception as e:fail(errors,'report',str(e))
 try:
  red=(w/'red_team.md').read_text().lower(); ids=set(re.findall(r'rt-\d{3}',red))
  if len(ids)<5:fail(errors,'red-team-findings',str(len(ids)))
  if not any(x in red for x in ['double count','double-count']):fail(errors,'red-team-double-count','missing')
  if not any(x in red for x in ['valuation','normalization']):fail(errors,'red-team-valuation','missing')
  if not any(x in red for x in ['source independence','independence cluster']):fail(errors,'red-team-source-independence','missing')
 except Exception as e:fail(errors,'red-team',str(e))
 try:
  snap=json.loads((w/'forecast_snapshot.json').read_text());need=['forecast_id','as_of','model_version','source_pack_hash','mechanism_weights','scenario_probabilities','outputs','breakpoints','human_required','confidence_and_limits'];miss=[k for k in need if k not in snap]
  if miss:fail(errors,'snapshot-fields',','.join(miss))
  if abs(sum(map(float,snap.get('mechanism_weights',{}).values()))-1)>1e-4:fail(errors,'mechanism-weights','not 1')
  if abs(sum(map(float,snap.get('scenario_probabilities',{}).values()))-1)>1e-4:fail(errors,'scenario-probs','not 1')
 except Exception as e:fail(errors,'snapshot',str(e))
 try:
  names=sheets(model)
  for k,aliases in SHEETS.items():
   if not any(any(a in n for a in aliases) for n in names):fail(errors,'workbook:'+k,str(names))
 except Exception as e:fail(errors,'workbook',str(e))
 result={'workspace':str(w),'passed':not errors,'strict':a.strict,'errors':len(errors),'warnings':0,'checks':checks,'error_details':errors}
 (w/'delivery_validation.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps(result,indent=2));return 0 if not errors else 2
if __name__=='__main__':raise SystemExit(main())
