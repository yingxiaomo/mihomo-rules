#!/usr/bin/env python3
"""Train four priors from Sage native events; PLD is never accepted."""
from __future__ import annotations
import argparse, hashlib, html, json, logging, os, shutil, sys, tempfile, time, traceback
from pathlib import Path
import lightgbm as lgb
import numpy as np
import pandas as pd
import requests
from sklearn.linear_model import LogisticRegression

SCHEMA_VERSION=2; SOURCE_SCHEMA="sage-native-events-v1"; FEATURE_SCHEMA="sage-prior-native-features-v1"
MAX_PRIOR_CONFIDENCE=.35; MIN_TRAIN_ROWS=20; MIN_VALID_ROWS=5; MIN_CONFIDENCE_ROWS=20
FEATURE_ORDER=["is_udp","is_tcp","port_log","asn_hash","host_hash","ip_hash","group_hash","node_type"]
DIMENSIONS=("connect","response","throughput","failure"); PARTITIONS=("train","early_stop","calibration","final_test")
MODEL_PARAMS={"verbosity":-1,"learning_rate":.035,"num_leaves":24,"min_data_in_leaf":12,"feature_fraction":.9,"bagging_fraction":.9,"bagging_freq":1,"lambda_l1":.1,"lambda_l2":.5,"seed":20261115,"feature_fraction_seed":20261115,"bagging_seed":20261115,"data_random_seed":20261115,"deterministic":True,"force_col_wise":True,"num_threads":1}

LOGGER = logging.getLogger("sage_prior_trainer")
DEFAULT_LOG_PATH = Path(__file__).with_name("training_sage.log")
MAX_TG_CHUNKS = 12

def setup_logging(path=DEFAULT_LOG_PATH):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 _close_logging();LOGGER.setLevel(logging.INFO);LOGGER.propagate=False
 formatter=logging.Formatter("%(message)s")
 file_handler=None;console=None
 try:
  file_handler=logging.FileHandler(path,mode="w",encoding="utf-8");file_handler.setFormatter(formatter)
  console=logging.StreamHandler(sys.stdout);console.setFormatter(formatter)
  LOGGER.addHandler(console);LOGGER.addHandler(file_handler)
 except Exception:
  for handler in (file_handler,console):
   if handler is not None:handler.close()
  LOGGER.handlers.clear();raise
 try:lgb.register_logger(LOGGER)
 except (AttributeError,TypeError):pass
 LOGGER.info(f"--- Sage 训练开始: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
 return path

def _close_logging():
 for handler in list(LOGGER.handlers):
  handler.flush();handler.close();LOGGER.removeHandler(handler)

def _separator(title):
 LOGGER.info("="*72);LOGGER.info(title);LOGGER.info("="*72)

def _send_telegram(text):
 token=os.environ.get("TG_BOT_TOKEN","");chat_id=os.environ.get("TG_CHAT_ID","")
 if not token or not chat_id:return False
 for attempt in range(1,4):
  try:
   response=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",json={"chat_id":chat_id,"text":text,"parse_mode":"HTML"},timeout=10)
   if response.status_code==200:
    try:api_ok=response.json().get("ok") is True
    except (requests.JSONDecodeError,ValueError,AttributeError):api_ok=False
    if api_ok:return True
    LOGGER.warning(f"⚠️ Telegram 发送失败: HTTP 200 但 Bot API ok!=true (attempt {attempt}/3)")
   delay=attempt
   if response.status_code==429:
    try:delay=max(1,min(10,int(response.json().get("parameters",{}).get("retry_after",attempt))))
    except (TypeError,ValueError,AttributeError):delay=attempt
   if response.status_code!=200:LOGGER.warning(f"⚠️ Telegram 发送失败: HTTP {response.status_code} (attempt {attempt}/3)")
  except Exception as exc:
   delay=attempt;LOGGER.warning(f"⚠️ Telegram 发送失败: {type(exc).__name__} (attempt {attempt}/3)")
  if attempt<3:time.sleep(delay)
 return False

def send_telegram_logs(header,log_path):
 if not os.environ.get("TG_BOT_TOKEN") or not os.environ.get("TG_CHAT_ID"):
  LOGGER.info("ℹ️ Telegram 未配置，已跳过训练日志推送");return
 messages=[header]
 try:content=Path(log_path).read_text(encoding="utf-8")
 except Exception as exc:LOGGER.warning(f"⚠️ 无法读取训练日志: {exc}");return
 chunks=[content[i:i+3500] for i in range(0,len(content),3500)]
 messages.extend(f"<pre>{html.escape(chunk)}</pre>" for chunk in chunks[:MAX_TG_CHUNKS])
 sent=0
 for index,message in enumerate(messages):
  sent+=int(_send_telegram(message))
  if index<len(messages)-1:time.sleep(.5)
 if sent==len(messages):LOGGER.info(f"📨 Telegram 日志推送完成: {sent}/{len(messages)} 条（日志 {min(len(chunks),MAX_TG_CHUNKS)}/{len(chunks)} 块）")
 else:LOGGER.warning(f"⚠️ Telegram 日志推送不完整: {sent}/{len(messages)} 条成功（日志 {min(len(chunks),MAX_TG_CHUNKS)}/{len(chunks)} 块）")

def _format_ns(value):
 if value is None:return "结束"
 return time.strftime("%Y-%m-%d %H:%M:%S UTC",time.gmtime(int(value)/1_000_000_000))

def _canonical(e): return json.dumps(e,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()

def load_native_events(data_dir):
 root=Path(data_dir)
 LOGGER.info("[步骤1] 加载并校验 Sage 原生事件")
 LOGGER.info(f"📂 数据目录: {root.resolve()}")
 compromised=set()
 for marker in [*root.rglob("*.jsonl.failed"),*root.rglob("*.jsonl.recovered")]:
  parts=marker.name.split(".")
  if len(parts)<3 or parts[0]!="sage-native-v1": raise ValueError(f"无法识别损坏采集文件 {marker}")
  compromised.add(parts[1])
 files=sorted(p for p in root.rglob("*.jsonl.ready") if p.is_file() and (len(p.name.split("."))<2 or p.name.split(".")[1] not in compromised))
 if not files: raise ValueError("未找到 Sage 原生 .jsonl.ready 数据")
 LOGGER.info(f"📦 可用闭合文件: {len(files)} 个 | 隔离 run: {len(compromised)} 个")
 by_id={};file_summaries=[]
 for path in files:
  header=footer=False; run_id=""; local=[]; footer_event=None
  with path.open(encoding="utf-8") as stream:
   for no,raw in enumerate(stream,1):
    if not raw.endswith("\n"): raise ValueError(f"{path}:{no} 非完整 JSONL")
    try:e=json.loads(raw)
    except json.JSONDecodeError as exc: raise ValueError(f"{path}:{no} JSON 无效") from exc
    if not isinstance(e,dict) or e.get("source_schema")!=SOURCE_SCHEMA: raise ValueError(f"{path}:{no} source_schema 不兼容")
    kind=e.get("event_type")
    if kind=="file_header":
     if header or footer or e.get("feature_schema")!=FEATURE_SCHEMA or not e.get("run_id"): raise ValueError(f"{path} header 无效")
     header=True; run_id=str(e["run_id"]); continue
    if kind=="file_footer":
     if not header or footer or str(e.get("run_id",""))!=run_id: raise ValueError(f"{path} footer 无效")
     if int(e.get("dropped_control",0)) or int(e.get("dropped_segment",0)) or int(e.get("incomplete_attempts",0)): raise ValueError(f"{path} producer 报告事件丢失")
     footer=True; footer_event=e; continue
    if not header or footer or str(e.get("run_id",""))!=run_id: raise ValueError(f"{path}:{no} 文件边界/run_id 无效")
    feat=e.get("features"); event_id=str(e.get("event_id","")); attempt_id=str(e.get("attempt_id",""))
    if e.get("feature_schema")!=FEATURE_SCHEMA or not event_id or not attempt_id: raise ValueError(f"{path}:{no} 事件 ABI 无效")
    if not isinstance(feat,list) or len(feat)!=8 or not np.isfinite(np.asarray(feat,float)).all(): raise ValueError(f"{path}:{no} feature ABI 无效")
    local.append(e)
  if not header or not footer or footer_event is None: raise ValueError(f"{path} 缺少 header/footer")
  sequences=[int(e["event_seq"]) for e in local]
  actual_first=min(sequences) if sequences else 0; actual_last=max(sequences) if sequences else 0
  if int(footer_event.get("written_events",-1))!=len(local) or int(footer_event.get("first_event_seq",-1))!=actual_first or int(footer_event.get("last_event_seq",-1))!=actual_last: raise ValueError(f"{path} footer 声明与事件内容不一致")
  file_summaries.append(f"{path.name}: run={run_id} | events={len(local)} | seq={actual_first}..{actual_last}")
  for e in local:
   payload=_canonical(e); event_id=str(e["event_id"]); old=by_id.get(event_id)
   if old and old[0]!=payload: raise ValueError(f"event_id {event_id} 冲突")
   by_id[event_id]=(payload,e)
 frame=pd.DataFrame([x[1] for x in by_id.values()])
 if frame.empty: raise ValueError("没有可训练事件")
 for col in ("event_seq","decision_unix_nano","observed_unix_nano"): frame[col]=pd.to_numeric(frame[col],errors="raise").astype(np.int64)
 for run,group in frame.groupby("run_id",sort=False):
  seq=np.sort(group.event_seq.to_numpy(np.int64))
  if len(seq) and seq[0]!=1: raise ValueError(f"run {run} event_seq 未从 1 开始")
  if len(seq)>1 and np.any(np.diff(seq)!=1): raise ValueError(f"run {run} event_seq 不连续")
 counts=frame.event_type.value_counts().sort_index()
 shown=file_summaries if len(file_summaries)<=6 else [*file_summaries[:3],f"... 省略 {len(file_summaries)-6} 个已通过文件 ...",*file_summaries[-3:]]
 for summary in shown:LOGGER.info(f"  ✓ {summary}")
 LOGGER.info(f"✅ 原生事件校验通过: {len(frame)} 条去重事件 | {frame.run_id.nunique()} 个 run | {frame.attempt_id.nunique()} 个 attempt")
 LOGGER.info("📊 事件类型: "+" | ".join(f"{kind}={int(count)}" for kind,count in counts.items()))
 LOGGER.info(f"🕒 时间范围: {_format_ns(frame.observed_unix_nano.min())} → {_format_ns(frame.observed_unix_nano.max())}")
 return frame.sort_values(["decision_unix_nano","attempt_id","event_seq"],kind="stable").reset_index(drop=True)

def build_features(events):
 rows=[]
 for i,v in events.features.items():
  if not isinstance(v,(list,tuple)) or len(v)!=8: raise ValueError(f"事件 {i} feature ABI 错误")
  a=np.asarray(v,float)
  if not np.isfinite(a).all(): raise ValueError(f"事件 {i} feature 非有限")
  rows.append(a)
 return pd.DataFrame(rows,columns=FEATURE_ORDER,index=events.index)

def _attempt_partitions(events):
 attempts=events.groupby("attempt_id",as_index=False).agg(decision_unix_nano=("decision_unix_nano","min")).sort_values(["decision_unix_nano","attempt_id"],kind="stable").reset_index(drop=True)
 minimum=MIN_TRAIN_ROWS+3*MIN_VALID_ROWS
 if len(attempts)<minimum: raise ValueError(f"独立 attempt 至少需要 {minimum} 条")
 a=max(MIN_TRAIN_ROWS,int(len(attempts)*.60)); b=max(a+MIN_VALID_ROWS,int(len(attempts)*.75)); c=max(b+MIN_VALID_ROWS,int(len(attempts)*.85)); c=min(c,len(attempts)-MIN_VALID_ROWS)
 if b>=c:b=c-MIN_VALID_ROWS
 cuts=(0,a,b,c,len(attempts)); attempts["partition"]=""
 for name,left,right in zip(PARTITIONS,cuts[:-1],cuts[1:]): attempts.loc[left:right-1,"partition"]=name
 bounds={name:(int(attempts.iloc[cuts[i+1]].decision_unix_nano) if cuts[i+1]<len(attempts) else None) for i,name in enumerate(PARTITIONS)}
 return attempts,bounds

def build_training_tables(events):
 LOGGER.info("[步骤2] 按 attempt 做全局时间切分并构造四维标签")
 required={"attempt_id","event_type","decision_unix_nano","observed_unix_nano","transport","features"}
 if required-set(events.columns): raise ValueError(f"缺少字段 {sorted(required-set(events.columns))}")
 attempts,bounds=_attempt_partitions(events); work=events.copy(); work["partition"]=work.attempt_id.map(attempts.set_index("attempt_id").partition); features=build_features(work)
 partition_counts=attempts.partition.value_counts()
 LOGGER.info(f"🧩 独立 attempt: {len(attempts)} | "+" | ".join(f"{name}={int(partition_counts.get(name,0))}" for name in PARTITIONS))
 LOGGER.info("🕒 attempt 分区依据=decision_unix_nano；分区上界: "+" | ".join(f"{name}<{_format_ns(bounds[name])}" for name in PARTITIONS))
 LOGGER.info("🧹 标签迟到过滤依据=observed_unix_nano；标签必须早于所属分区上界")
 for name in FEATURE_ORDER: work[name]=features[name]
 empty=pd.Series(index=work.index,dtype=object)
 specs={"connect":(work.event_type.eq("setup")&work.get("setup_status",empty).eq("returned"),"log_milliseconds"),"response":(work.event_type.eq("response")&work.transport.eq("tcp"),"log_milliseconds"),"throughput":(work.event_type.eq("throughput")&work.transport.eq("tcp"),"log_bits_per_second"),"failure":(work.event_type.eq("outcome")&work.get("outcome",empty).isin(["success","failure"]),"probability")}
 tables={}
 for name,(mask,unit) in specs.items():
  table=work.loc[mask].copy()
  if table.empty: raise ValueError(f"{name} 没有有效标签")
  candidates=len(table)
  value=pd.to_numeric(table.value,errors="coerce")
  if name in ("connect","response"): valid=value.gt(0)&value.le(30000); table["label"]=np.log(value.clip(1,30000))
  elif name=="throughput": valid=value.ge(8192)&value.le(1e9)&pd.to_numeric(table.bytes,errors="coerce").ge(65536)&pd.to_numeric(table.elapsed_nano,errors="coerce").ge(2e9); table["label"]=np.log(value.clip(8192,1e9))
  else: valid=table.outcome.isin(["success","failure"]); table["label"]=table.outcome.map({"success":0.,"failure":1.})
  table=table.loc[valid&table[FEATURE_ORDER].notna().all(axis=1)].copy();invalid_dropped=candidates-len(table);before_late=len(table);keep=pd.Series(True,index=table.index)
  for part,upper in bounds.items():
   if upper is not None: keep &= ~table.partition.eq(part)|table.observed_unix_nano.lt(upper)
  table=table.loc[keep].copy();late_dropped=before_late-len(table);table["weight"]=1.
  if name=="throughput": table["weight"]=1./table.groupby("attempt_id").attempt_id.transform("count").clip(lower=1)
  table["unit"]=unit; tables[name]=table.sort_values(["decision_unix_nano","attempt_id","event_seq"],kind="stable").reset_index(drop=True)
  by_partition=tables[name].groupby("partition").attempt_id.nunique()
  LOGGER.info(f"  • {name:<10} candidate={candidates} → invalid/range dropped={invalid_dropped} → late dropped={late_dropped} → retained={len(tables[name])} | 独立 attempt={tables[name].attempt_id.nunique()} | "+" | ".join(f"{part}={int(by_partition.get(part,0))}" for part in PARTITIONS))
  if name=="throughput":LOGGER.info("    throughput 权重: 同一 attempt 内各窗口权重和固定为 1")
 return attempts,tables

def _confidence(a,b):
 if not(np.isfinite(a) and np.isfinite(b)) or b<=1e-12:return 0.
 return round(min(MAX_PRIOR_CONFIDENCE,max(0.,1-a/b)),6)

def _weighted_median(v,w):
 v,w=np.asarray(v,float),np.asarray(w,float); valid=np.isfinite(v)&np.isfinite(w)&(w>0)
 if not np.any(valid):raise ValueError("加权中位数需要正权重")
 v,w=v[valid],w[valid]; order=np.argsort(v,kind="stable");v,w=v[order],w[order]
 return float(v[min(int(np.searchsorted(np.cumsum(w),w.sum()*.5,side="left")),len(v)-1)])

def train_dimension(name,table):
 _separator(f"[步骤3] 训练 {name} 维度")
 parts={p:table.loc[table.partition.eq(p)].reset_index(drop=True) for p in PARTITIONS}
 evidence={p:int(frame.attempt_id.nunique()) for p,frame in parts.items()}
 if evidence["train"]<MIN_TRAIN_ROWS or any(evidence[p]<MIN_VALID_ROWS for p in PARTITIONS[1:]):raise ValueError(f"{name} 独立 attempt 分区不足: {evidence}")
 objective,metric=("binary","binary_logloss") if name=="failure" else ("regression_l2","l2");params={**MODEL_PARAMS,"objective":objective,"metric":metric};train,early=parts["train"],parts["early_stop"]
 LOGGER.info("📊 独立 attempt: "+" | ".join(f"{part}={evidence[part]}" for part in PARTITIONS))
 LOGGER.info(f"🏷️ 标签: rows={len(table)} | min={table.label.min():.6f} | p50={table.label.median():.6f} | p95={table.label.quantile(.95):.6f} | max={table.label.max():.6f}")
 if name=="failure":LOGGER.info(f"⚖️ 失败标签: success={int((table.label==0).sum())} | failure={int((table.label==1).sum())}")
 LOGGER.info(f"🌲 LightGBM: objective={objective} | metric={metric} | max_rounds=600 | early_stopping=40 | learning_rate={params['learning_rate']} | features={len(FEATURE_ORDER)}")
 ds=lgb.Dataset(train[FEATURE_ORDER],label=train.label,weight=train.weight,feature_name=FEATURE_ORDER); es=lgb.Dataset(early[FEATURE_ORDER],label=early.label,weight=early.weight,reference=ds,feature_name=FEATURE_ORDER)
 model=lgb.train(params,ds,num_boost_round=600,valid_sets=[es],valid_names=["early_stop"],callbacks=[lgb.early_stopping(40,verbose=True),lgb.log_evaluation(50)])
 LOGGER.info(f"🏆 {name} 最佳迭代: {model.best_iteration}")
 final=parts["final_test"];truth,weights=final.label.to_numpy(float),final.weight.to_numpy(float);ty,tw=train.label.to_numpy(float),train.weight.to_numpy(float);transform,scale,bias="identity",1.,0.
 if name=="failure":
  transform="sigmoid";cal=parts["calibration"];cy,cw=cal.label.to_numpy(float),cal.weight.to_numpy(float);raw=np.asarray(model.predict(cal[FEATURE_ORDER],num_iteration=model.best_iteration,raw_score=True),float);calibrated=len(np.unique(cy))==2
  if calibrated:
   lr=LogisticRegression(C=1e6,solver="lbfgs",random_state=20261115).fit(raw.reshape(-1,1),cy,sample_weight=cw);candidate_scale,candidate_bias=float(lr.coef_[0,0]),float(lr.intercept_[0])
   if candidate_scale>0:scale,bias=candidate_scale,candidate_bias
   else:calibrated=False
  raw=np.asarray(model.predict(final[FEATURE_ORDER],num_iteration=model.best_iteration,raw_score=True),float);pred=1/(1+np.exp(-np.clip(scale*raw+bias,-40,40)));base=float(np.average(ty,weights=tw));error=float(np.average((truth-pred)**2,weights=weights));base_error=float(np.average((truth-base)**2,weights=weights));metric_name="brier"
  baseline_strategy="train weighted failure prevalence"
 else:
  calibrated=True;pred=np.asarray(model.predict(final[FEATURE_ORDER],num_iteration=model.best_iteration),float);base=_weighted_median(ty,tw);error=float(np.average(abs(truth-pred),weights=weights));base_error=float(np.average(abs(truth-base),weights=weights));metric_name="mae_log"
  baseline_strategy="train weighted median"
 confidence=_confidence(error,base_error) if calibrated and evidence["final_test"]>=MIN_CONFIDENCE_ROWS else 0.
 improvement=(1-error/base_error) if np.isfinite(base_error) and base_error>1e-12 else float("nan")
 LOGGER.info(f"📏 朴素基线: strategy={baseline_strategy} | prediction={base:.9f}")
 LOGGER.info(f"🧪 最终验证: metric={metric_name} | model_error={error:.9f} | baseline_error={base_error:.9f} | improvement={improvement:.2%}")
 if not calibrated:reason="校准集类别不足或校准斜率无效"
 elif evidence["final_test"]<MIN_CONFIDENCE_ROWS:reason=f"final_test 独立 attempt {evidence['final_test']} < {MIN_CONFIDENCE_ROWS}"
 elif not np.isfinite(error):reason="model_error 非有限"
 elif not np.isfinite(base_error) or base_error<=1e-12:reason="baseline_error 非法或无区分度"
 elif improvement<=0:reason="模型未优于朴素基线"
 else:reason="通过"
 LOGGER.info(f"🛡️ confidence={confidence:.6f} / cap={MAX_PRIOR_CONFIDENCE:.2f} | gate={reason}")
 if name=="failure":LOGGER.info(f"🎛️ failure calibration: calibrated={calibrated} | scale={scale:.12f} | bias={bias:.12f}")
 return model,{"unit":str(table.unit.iloc[0]),"confidence":confidence,"train_samples":evidence["train"],"early_stop_samples":evidence["early_stop"],"calibration_samples":evidence["calibration"],"validation_samples":evidence["final_test"],"validation_metric":metric_name,"validation_error":round(error,9),"baseline_error":round(base_error,9),"output_transform":transform,"calibration_scale":round(scale,12),"calibration_bias":round(bias,12)}

def _sha256(path):
 h=hashlib.sha256()
 with Path(path).open("rb") as f:
  for block in iter(lambda:f.read(1<<20),b""):h.update(block)
 return h.hexdigest()

def save_bundle(output,attempts,models,metadata):
 _separator("[步骤4] 生成原子化四维模型包")
 output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True);stem=output.stem;dimensions={};staging=Path(tempfile.mkdtemp(prefix=f".{stem}-",dir=output.parent));created=[]
 try:
  for name in DIMENSIONS:
   staged=staging/f"{stem}-{name}.lgb";models[name].save_model(str(staged),num_iteration=models[name].best_iteration);digest=_sha256(staged);target=output.with_name(f"{stem}-{name}-{digest[:16]}.lgb")
   if target.exists() and _sha256(target)!=digest:raise ValueError("模型冲突")
   if not target.exists():os.replace(staged,target);created.append(target)
   dimensions[name]={**metadata[name],"model":target.name,"sha256":digest}
   LOGGER.info(f"  ✓ {name:<10} {target.name} | {target.stat().st_size} bytes | sha256={digest}")
  manifest={"schema_version":SCHEMA_VERSION,"feature_schema":FEATURE_SCHEMA,"source_schema":SOURCE_SCHEMA,"feature_order":FEATURE_ORDER,"generated_from_event_unix":int(attempts.decision_unix_nano.max()//1_000_000_000),"max_prior_confidence":MAX_PRIOR_CONFIDENCE,"dimensions":dimensions}
  temp=output.with_suffix(output.suffix+".tmp");temp.write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8");os.replace(temp,output)
  LOGGER.info(f"📦 manifest: {output} | {output.stat().st_size} bytes | sha256={_sha256(output)} | generated_from={manifest['generated_from_event_unix']}")
  LOGGER.info("📈 四维 confidence: "+" | ".join(f"{name}={dimensions[name]['confidence']:.6f}" for name in DIMENSIONS))
  return manifest
 except Exception:
  for p in created:p.unlink(missing_ok=True)
  raise
 finally:shutil.rmtree(staging,ignore_errors=True)

def run_training(argv=None):
 p=argparse.ArgumentParser();p.add_argument("--data-dir",type=Path,default=Path(__file__).parent/"sage-data");p.add_argument("--output",type=Path,default=Path(__file__).parent/"SagePrior.json");p.add_argument("--log-file",type=Path,default=DEFAULT_LOG_PATH);a=p.parse_args(argv)
 log_path=setup_logging(a.log_file);started=time.monotonic();_separator("Sage 原生四维先验训练")
 LOGGER.info(f"📋 source_schema={SOURCE_SCHEMA} | feature_schema={FEATURE_SCHEMA} | features={len(FEATURE_ORDER)}")
 try:
  events=load_native_events(a.data_dir);attempts,tables=build_training_tables(events);models={};metadata={}
  for name in DIMENSIONS:models[name],metadata[name]=train_dimension(name,tables[name])
  manifest=save_bundle(a.output,attempts,models,metadata);elapsed=time.monotonic()-started
  _separator("训练完成")
  LOGGER.info(f"🎉 Sage 四维先验已生成: {Path(a.output).resolve()} | elapsed={elapsed:.2f}s")
  header=(f"✅ <b>Sage 四维先验训练成功</b>\n📊 事件: <code>{len(events)}</code> | attempt: <code>{len(attempts)}</code>\n"+"\n".join(f"{name}: <code>{manifest['dimensions'][name]['confidence']:.6f}</code>" for name in DIMENSIONS))
  send_telegram_logs(header,log_path);return manifest
 except Exception:
  LOGGER.error(traceback.format_exc())
  send_telegram_logs(f"❌ <b>Sage 四维先验训练失败</b>\n<code>{html.escape(traceback.format_exc()[-500:])}</code>",log_path)
  raise
 finally:_close_logging()
if __name__=="__main__":run_training()
