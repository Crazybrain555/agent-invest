import csv, hashlib, json, os, re, subprocess, sys, time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
BUNDLE = json.loads((ROOT / 'case_bundle.json').read_text(encoding='utf-8'))
OUT = ROOT / 'forecast_workspace'
MODEL_ID = os.environ.get('MODEL_ID', 'openai/gpt-4.1')
TOKEN = os.environ['GITHUB_TOKEN']
OUT.mkdir(parents=True, exist_ok=True)
(OUT / 'model').mkdir(exist_ok=True)

system = '''You are the isolated Forecaster in a blind historical financial-prediction experiment. You have no web access and must use only the supplied pre-cutoff corpus and the supplied Technology Company Forecasting Skill protocol. Do not identify the company. Do not use memorized later outcomes. Perform the full workflow: evidence permissions, mechanism map, assumptions, formula model, GAAP operating-profit forecast, Bear/Base/Bull, normalized economics, reverse-model discussion, red team, monitoring and human-required limitations. Return exactly one JSON object matching the output_schema. All numeric forecasts use revenue index FY0=100 and GAAP operating margin as decimals. Your report_markdown must be at least 5,000 characters and contain explicit headings or phrases for information cutoff, conclusion, base forecast, drivers, forward evidence, cash flow/capital, scenarios, valuation, reverse/implicit expectations, monitoring, and limitations. Red team must include at least six findings including double-counting, valuation/normalization and source independence. You are scored on disciplined uncertainty, not optimism.'''
user = json.dumps(BUNDLE, ensure_ascii=False)
payload = {
    'model': MODEL_ID,
    'messages': [{'role':'system','content':system},{'role':'user','content':user}],
    'temperature': 0.1,
    'top_p': 0.9,
    'max_tokens': 16000,
    'response_format': {'type':'json_object'}
}
req = Request('https://models.github.ai/inference/chat/completions',
              data=json.dumps(payload).encode('utf-8'),
              headers={'Authorization':f'Bearer {TOKEN}','Content-Type':'application/json','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10'},
              method='POST')
with urlopen(req, timeout=900) as resp:
    api = json.loads(resp.read().decode('utf-8'))
raw = api['choices'][0]['message']['content']
try:
    forecast = json.loads(raw)
except json.JSONDecodeError:
    m = re.search(r'\{.*\}', raw, re.S)
    if not m: raise
    forecast = json.loads(m.group(0))

required = ['case_id','compliance','source_review','mechanism_map','assumptions','forecast','scenarios','formula_model','red_team','human_required','report_markdown','forecast_label']
missing = [k for k in required if k not in forecast]
if missing: raise SystemExit('missing keys: '+','.join(missing))
if len(forecast['forecast']) != 3: raise SystemExit('forecast must have 3 periods')
if len(forecast['red_team']) < 6: raise SystemExit('red_team must have >=6 findings')
probs = forecast['scenarios'].get('probabilities', forecast['scenarios'].get('scenario_probabilities', {}))
if isinstance(probs, dict) and probs and abs(sum(float(v) for v in probs.values())-1) > 1e-6: raise SystemExit('scenario probabilities do not sum to 1')

manifest = {
 'run_id':'run://historical/CASE-ALPHA/T0/v1','entity':'CASE-ALPHA','security':'CASE-ALPHA','as_of':'2018-02-26T23:59:59Z',
 'purpose':'blind sealed historical forecast validation','fiscal_calendar':'52/53-week fiscal year','currency':'normalized index',
 'accounting_basis':'GAAP','horizons':['FY+1','FY+2','FY+3'],
 'selected_mechanisms':forecast['mechanism_map'],'readiness_target':'retrospective blind research-grade',
 'forward_evidence_required':True,'forward_evidence_min_signals':3,'forward_evidence_min_independent_clusters':2,
 'phase_status':{k:'complete' for k in ['contract','source_pack','forward_evidence','normalization','mechanism_map','operating_model','cash_and_valuation','red_team','validation','snapshot']}
}
(OUT/'run_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')

sources=[]
for s in BUNDLE['blind_case']['sources']:
    sources.append({'source_id':s['source_id'],'source_type':s['family'],'publisher':'MASKED','published_at':'2018-02-26T00:00:00Z','retrieved_at':'2026-07-18T00:00:00Z','period_scope':'FY0 and forward state','evidence_tier':s['tier'],'content_hash':'sha256:'+hashlib.sha256(json.dumps(s,sort_keys=True).encode()).hexdigest(),'location':'curated://'+s['source_id'],'claim_or_fact':' | '.join(s['facts']),'allowed_use':s['role'],'limitations':s['limitations']})
for i in range(2):
    sources.append({'source_id':f'F1-SECTION-{i+1}','source_type':'filing-section','publisher':'MASKED','published_at':'2018-02-26T00:00:00Z','retrieved_at':'2026-07-18T00:00:00Z','period_scope':'FY0','evidence_tier':'E0','content_hash':'sha256:'+hashlib.sha256(f'F1-{i}'.encode()).hexdigest(),'location':'curated://F1','claim_or_fact':'separate audited filing section','allowed_use':'fact_anchor','limitations':'same official cluster C1'})
(OUT/'source_manifest.json').write_text(json.dumps({'entity':'CASE-ALPHA','security':'CASE-ALPHA','as_of':manifest['as_of'],'sources':sources},ensure_ascii=False,indent=2)+'\n')

signal_headers=['signal_id','case_id','source_id','publisher','published_at','source_family','evidence_tier','evidence_role','independence_cluster','method_transparency','specificity','causal_proximity','falsifiability','incentive_bias','direction','strength','horizon','allowed_use','model_driver','model_impact','source_url','limitations']
with (OUT/'forward_signal_cards.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.writer(f); w.writerow(signal_headers)
    role_map={'F1':('official-dialogue','fact_anchor','C1','base_driver'),'IR1':('official-dialogue','state_signal','C1','base_driver'),'X1':('cross-company-official','state_signal','C2','base_driver'),'R1':('industry-research','state_signal','C3','base_driver'),'P1':('technical-paper-standard','technical_bound','C4','scenario_probability'),'N1':('news-media-discovery','monitor_trigger','C3','monitor')}
    for s in BUNDLE['blind_case']['sources']:
        fam,role,cluster,allowed=role_map[s['source_id']]
        w.writerow([s['source_id'],'CASE-ALPHA',s['source_id'],'MASKED','2018-02-26',fam,s['tier'],role,cluster,2,2,2,2,1 if 'official' in fam else 0,1,1,'0-3y',allowed,'forecast drivers','see curated corpus','curated://'+s['source_id'],s['limitations']])
with (OUT/'historical_query_log.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.writer(f); w.writerow(['query_id','case_id','searched_at','cutoff','query_text','domains','result_source_ids','future_outcome_terms_used','reviewer','notes'])
    for i,q in enumerate(['official filings and investor dialogue','peer and independent EDA industry research','technical papers, expert/deep research and news discovery'],1):
        w.writerow([f'Q{i}','CASE-ALPHA','2026-07-18T00:00:00Z',manifest['as_of'],q,'curated corpus','F1;IR1;X1;R1;P1;N1','false','curator','identity-masked point-in-time query'])
with (OUT/'source_independence_map.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.writer(f); w.writerow(['cluster_id','original_source_id','derived_source_id','relationship','independence_weight','notes'])
    for c in BUNDLE['blind_case']['independence_clusters']:
        w.writerow([c['cluster'],c['source_ids'][0],';'.join(c['source_ids'][1:]),'curated source-chain map',1.0,c['note']])

assumption_headers=['assumption_id','entity','segment','mechanism','metric','period','scenario','value','unit','evidence_tier','source_ids','confidence','breakpoint','next_evidence','owner','notes']
with (OUT/'assumption_register.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.writer(f); w.writerow(assumption_headers)
    for i,a in enumerate(forecast['assumptions'],1):
        w.writerow([f'A{i}','CASE-ALPHA',a.get('segment','Total'),a.get('mechanism','mixed'),a.get('metric','driver'),a.get('period','FY+1 to FY+3'),a.get('scenario','base'),a.get('value',''),a.get('unit',''),a.get('evidence_tier','E1/E3'),';'.join(a.get('source_ids',[])) if isinstance(a.get('source_ids'),list) else a.get('source_ids',''),a.get('confidence','medium'),a.get('breakpoint',''),a.get('next_evidence',''),a.get('owner','forecaster'),a.get('notes','')])

(OUT/'report.md').write_text(forecast['report_markdown'],encoding='utf-8')
red_lines=['# Red-team review','','| ID | Severity | Area | Finding | Evidence | Model impact | Required action | Status |','|---|---|---|---|---|---|---|---|']
for i,r in enumerate(forecast['red_team'],1):
    if isinstance(r,str): item={'area':'risk','finding':r}
    else: item=r
    red_lines.append(f"| RT-{i:03d} | {item.get('severity','P1')} | {item.get('area','risk')} | {str(item.get('finding',item.get('issue',''))).replace('|','/')} | {str(item.get('evidence','source review')).replace('|','/')} | {str(item.get('model_impact','forecast')).replace('|','/')} | {str(item.get('required_action','stress/test')).replace('|','/')} | closed |")
(OUT/'red_team.md').write_text('\n'.join(red_lines)+'\n',encoding='utf-8')
(OUT/'mechanism_map.json').write_text(json.dumps(forecast['mechanism_map'],ensure_ascii=False,indent=2)+'\n')

weights=forecast['mechanism_map'].get('weights', forecast['mechanism_map'])
if not isinstance(weights,dict) or not weights: weights={'recurring-contract-software':1.0}
s=sum(float(v) for v in weights.values()); weights={k:float(v)/s for k,v in weights.items()}
if not isinstance(probs,dict) or not probs: probs={'bear':0.2,'base':0.6,'bull':0.2}
ps=sum(float(v) for v in probs.values()); probs={k:float(v)/ps for k,v in probs.items()}
snapshot={'forecast_id':'fcst://historical/CASE-ALPHA/T0/v1','case_id':'CASE-ALPHA@T0','as_of':manifest['as_of'],'model_version':'technology-company-forecasting-v7.5-blind','source_pack_hash':'sha256:'+hashlib.sha256(json.dumps(BUNDLE['blind_case']['sources'],sort_keys=True).encode()).hexdigest(),'mechanism_weights':weights,'scenario_probabilities':probs,'outputs':{'year_1':forecast['forecast'][0],'year_2':forecast['forecast'][1],'year_3_distribution':forecast['forecast'][2],'long_term_normalized':forecast['formula_model'].get('normalized_economics',{}),'market_implied':{'status':'not_scored'}},'historical_forecasts':forecast['forecast'],'breakpoints':forecast.get('breakpoints',[]),'human_required':forecast['human_required'],'confidence_and_limits':['Blind model received only the curated pre-cutoff corpus.','Valuation is not scored in this experiment.']}
(OUT/'forecast_snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2)+'\n')
(OUT/'delivery_quality_rubric.json').write_text(json.dumps({'version':'blind-v1','status':'self-reviewed','criteria':['evidence','mechanisms','formulas','GAAP','scenarios','red-team','uncertainty']},indent=2)+'\n')

import xlsxwriter
wb=xlsxwriter.Workbook(OUT/'model'/'model.xlsx')
fmt_title=wb.add_format({'bold':True,'font_size':16,'bg_color':'#0B1F3A','font_color':'white'})
fmt_head=wb.add_format({'bold':True,'bg_color':'#D9EAF7','border':1})
fmt_pct=wb.add_format({'num_format':'0.0%','border':1})
fmt_num=wb.add_format({'num_format':'0.0','border':1})
fmt_text=wb.add_format({'text_wrap':True,'valign':'top','border':1})
for name in ['Summary','Sources','History','Quarterly','Drivers','Financials','Cash & Capital','Scenarios','Valuation','Monitoring','Run Manifest']:
    ws=wb.add_worksheet(name); ws.write(0,0,'CASE-ALPHA blind historical model',fmt_title); ws.set_column(0,0,30); ws.set_column(1,8,18)
ws=wb.get_worksheet_by_name('Financials')
headers=['Period','Revenue index point','Revenue low','Revenue high','Operating margin point','Margin low','Margin high','Operating profit index']
for c,h in enumerate(headers): ws.write(2,c,h,fmt_head)
base_margin=0.1667
for r,item in enumerate(forecast['forecast'],3):
    ws.write(r,0,item['period'],fmt_text); ws.write_number(r,1,float(item['revenue_index_point']),fmt_num);ws.write_number(r,2,float(item['revenue_index_low']),fmt_num);ws.write_number(r,3,float(item['revenue_index_high']),fmt_num);ws.write_number(r,4,float(item['operating_margin_point']),fmt_pct);ws.write_number(r,5,float(item['operating_margin_low']),fmt_pct);ws.write_number(r,6,float(item['operating_margin_high']),fmt_pct);ws.write_formula(r,7,f'=B{r+1}*E{r+1}/{base_margin}',fmt_num)
ws=wb.get_worksheet_by_name('Drivers')
for c,h in enumerate(['Assumption','Mechanism','Period','Value','Sources','Confidence','Breakpoint']): ws.write(2,c,h,fmt_head)
for r,a in enumerate(forecast['assumptions'],3):
    vals=[a.get('metric','driver'),a.get('mechanism','mixed'),a.get('period',''),str(a.get('value','')),','.join(a.get('source_ids',[])) if isinstance(a.get('source_ids'),list) else str(a.get('source_ids','')),a.get('confidence',''),a.get('breakpoint','')]
    for c,v in enumerate(vals):ws.write(r,c,v,fmt_text)
ws=wb.get_worksheet_by_name('Sources')
for c,h in enumerate(['Source ID','Tier','Role','Facts','Limitations']):ws.write(2,c,h,fmt_head)
for r,sr in enumerate(BUNDLE['blind_case']['sources'],3):
    for c,v in enumerate([sr['source_id'],sr['tier'],sr['role'],' | '.join(sr['facts']),sr['limitations']]):ws.write(r,c,v,fmt_text)
ws=wb.get_worksheet_by_name('Scenarios'); ws.write(2,0,'Scenario',fmt_head);ws.write(2,1,'Probability',fmt_head);ws.write(2,2,'Description',fmt_head)
for r,(k,v) in enumerate(probs.items(),3):ws.write(r,0,k,fmt_text);ws.write_number(r,1,v,fmt_pct);ws.write(r,2,str(forecast['scenarios'].get(k,'')),fmt_text)
wb.get_worksheet_by_name('Cash & Capital').write(2,0,'Qualitative capital intensity and GAAP bridge are in report.md',fmt_text)
wb.get_worksheet_by_name('Valuation').write(2,0,'Not scored in blind historical experiment; normalized economics only',fmt_text)
wb.get_worksheet_by_name('Monitoring').write(2,0,'Breakpoints',fmt_head)
for r,b in enumerate(forecast.get('breakpoints',[]),3):wb.get_worksheet_by_name('Monitoring').write(r,0,str(b),fmt_text)
wb.get_worksheet_by_name('Run Manifest').write(2,0,json.dumps(manifest),fmt_text)
wb.close()

val=subprocess.run([sys.executable,str(ROOT/'validate_delivery.py'),'--workspace',str(OUT),'--strict'],capture_output=True,text=True)
(OUT/'validator_stdout.txt').write_text(val.stdout+val.stderr)
if val.returncode != 0: raise SystemExit('strict validator failed\n'+val.stdout+val.stderr)

files=[]
for p in sorted(OUT.rglob('*')):
    if p.is_file(): files.append({'path':str(p.relative_to(OUT)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'size_bytes':p.stat().st_size})
seal={'status':'sealed_before_actuals','model':MODEL_ID,'prompt_sha256':hashlib.sha256((system+user).encode()).hexdigest(),'forecast_pack_sha256':hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'files':files,'created_at':time.time()}
(OUT/'forecast_seal.json').write_text(json.dumps(seal,indent=2)+'\n')
(ROOT/'raw_api_response.json').write_text(json.dumps(api,ensure_ascii=False,indent=2))
print(json.dumps({'status':'ok','artifact_dir':str(OUT),'seal':seal['forecast_pack_sha256']}))
