import hashlib, json, math, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from smart_trainer.train_sage_prior import ALGORITHM_SIGNATURE, DIMENSIONS, FAILURE_MIN_CLASS_ATTEMPTS, FEATURE_ORDER, FEATURE_SCHEMA, LOGGER, MAX_PRIOR_CONFIDENCE, SOURCE_SCHEMA, _close_logging, _weighted_median, build_features, build_training_tables, load_native_events, preflight_training_tables, run_training, save_bundle, save_training_evidence, setup_logging, train_dimension

def feature(i): return [0.,1.,math.log1p(443),float(i%97),float(i%89),float(i%83),52.,3.]
def events(count=180, failure_every=7):
 out=[]; seq=0; base=1_800_000_000_000_000_000
 for i in range(count):
  attempt=f"run:a{i+1}"; decision=base+i*10_000_000_000
  common={"source_schema":SOURCE_SCHEMA,"feature_schema":FEATURE_SCHEMA,"run_id":"run","attempt_id":attempt,"decision_unix_nano":decision,"group_id":"g","node_id":f"n{i%7}","node_id_stable":True,"transport":"tcp","scene":"default","features":feature(i)}
  failed=i%failure_every==0
  rows=[("decision",{}),("setup",{"setup_status":"returned","metric":"connect","value":40.+i%23,"elapsed_nano":int((40+i%23)*1e6)}),("response",{"metric":"response","value":65.+i%31,"elapsed_nano":int((65+i%31)*1e6)}),("throughput",{"metric":"throughput","value":1e6+(i%17)*1e5,"bytes":300000,"elapsed_nano":2500000000,"segment_index":1}),("throughput",{"metric":"throughput","value":1.1e6+(i%17)*1e5,"bytes":350000,"elapsed_nano":2600000000,"segment_index":2}),("outcome",{"metric":"failure","outcome":"failure" if failed else "success","value":1. if failed else 0.}),("attempt_end",{"end_reason":"closed","total_bytes":650000})]
  for offset,(kind,extra) in enumerate(rows):
   seq+=1; out.append({**common,"event_seq":seq,"event_id":f"run:{seq}","event_type":kind,"observed_unix_nano":decision+(offset+1)*100000000,**extra})
 return pd.DataFrame(out)
def write_ready(root, frame, dropped=0, incomplete=0):
 sequence=frame["event_seq"].astype(int)
 path=Path(root)/"sage-native-v1.run.1.1.jsonl.ready"; header={"source_schema":SOURCE_SCHEMA,"feature_schema":FEATURE_SCHEMA,"run_id":"run","event_type":"file_header","observed_unix_nano":1}; footer={"source_schema":SOURCE_SCHEMA,"run_id":"run","event_type":"file_footer","observed_unix_nano":2,"dropped_control":0,"dropped_segment":dropped,"incomplete_attempts":incomplete,"written_events":len(frame),"first_event_seq":int(sequence.min()) if len(sequence) else 0,"last_event_seq":int(sequence.max()) if len(sequence) else 0}
 with path.open("w",encoding="utf-8",newline="\n") as f:
  f.write(json.dumps(header,separators=(",",":"))+"\n")
  for row in frame.to_dict("records"):f.write(json.dumps(row,separators=(",",":"))+"\n")
  f.write(json.dumps(footer,separators=(",",":"))+"\n")
 return path
class SchemaTests(unittest.TestCase):
 def test_features_are_frozen(self):
  frame=events(35).iloc[:2].copy(); got=build_features(frame); self.assertEqual(got.columns.tolist(),FEATURE_ORDER); self.assertEqual(got.iloc[0].tolist(),feature(0)); frame.at[0,"value"]=999; pd.testing.assert_frame_equal(got,build_features(frame))
 def test_complete_lossless_native_only(self):
  with tempfile.TemporaryDirectory() as tmp:
   write_ready(tmp,events(35)); self.assertEqual(len(load_native_events(Path(tmp))),245); Path(tmp,"ignored.jsonl.open").write_text("bad\n"); self.assertEqual(len(load_native_events(Path(tmp))),245)
  with tempfile.TemporaryDirectory() as tmp:
   write_ready(tmp,events(35),1)
   with self.assertRaisesRegex(ValueError,"事件丢失"):load_native_events(Path(tmp))
   with tempfile.TemporaryDirectory() as tmp:
    write_ready(tmp,events(35),incomplete=1)
    with self.assertRaisesRegex(ValueError,"事件丢失"):load_native_events(Path(tmp))
   with tempfile.TemporaryDirectory() as tmp:
    p=write_ready(tmp,events(35)); lines=p.read_text().splitlines(); p.write_text("\n".join([lines[0],*lines[2:]])+"\n")
    with self.assertRaisesRegex(ValueError,"footer 声明"):load_native_events(Path(tmp))
  with tempfile.TemporaryDirectory() as tmp:
   p=write_ready(tmp,events(35)); lines=p.read_text().splitlines(); p.write_text("\n".join(lines[:-1])+"\n")
   with self.assertRaisesRegex(ValueError,"header/footer"):load_native_events(Path(tmp))
  with tempfile.TemporaryDirectory() as tmp:
   Path(tmp,"smart_samples.csv").write_text("id,success\n1,1\n")
   with self.assertRaisesRegex(ValueError,"原生"):load_native_events(Path(tmp))
 def test_labels_and_weights(self):
  attempts,tables=build_training_tables(events(80)); self.assertEqual(len(attempts),80); self.assertEqual(set(tables),set(DIMENSIONS)); self.assertEqual(tables["failure"].label.drop_duplicates().sort_values().tolist(),[0.,1.]); self.assertTrue(np.allclose(tables["throughput"].groupby("attempt_id").weight.sum().to_numpy(),1.)); self.assertNotEqual(float(tables["connect"].label.iloc[0]),float(tables["response"].label.iloc[0]))
 def test_neutral_and_missing_are_not_zero(self):
  frame=events(40); first=frame.at[0,"attempt_id"]; frame.loc[(frame.attempt_id==first)&(frame.event_type=="outcome"),["outcome","value"]]=["neutral",0.]; frame.loc[(frame.attempt_id==first)&(frame.event_type=="setup"),"setup_status"]="error"; _,tables=build_training_tables(frame); self.assertNotIn(first,set(tables["failure"].attempt_id)); self.assertNotIn(first,set(tables["connect"].attempt_id)); self.assertIn(first,set(tables["response"].attempt_id))
 def test_sparse_head_uses_its_own_label_attempts(self):
  frame=events(260)
  keep={f"run:a{i+1}" for i in range(104)}
  frame=frame.loc[~frame.event_type.eq("throughput")|frame.attempt_id.isin(keep)].copy()
  _,tables=build_training_tables(frame)
  counts=tables["throughput"].groupby("partition").attempt_id.nunique().to_dict()
  self.assertEqual([counts.get(name,0) for name in ("train","early_stop","calibration","final_test")],[62,16,10,16])
  self.assertGreater(tables["connect"].decision_unix_nano.max(),tables["throughput"].decision_unix_nano.max())
 def test_observed_time_purge_is_strict_and_does_not_rebalance(self):
  frame=events(80)
  _,before=build_training_tables(frame)
  upper=before["throughput"].attrs["partition_boundaries"]["train"]["end_exclusive_decision_unix_nano"]
  victim=before["throughput"].loc[before["throughput"].partition.eq("train"),"attempt_id"].iloc[-1]
  mask=frame.attempt_id.eq(victim)&frame.event_type.eq("throughput")
  frame.loc[mask,"observed_unix_nano"]=upper
  _,after=build_training_tables(frame)
  self.assertNotIn(victim,set(after["throughput"].attempt_id))
  self.assertEqual(after["throughput"].attrs["partition_boundaries"],before["throughput"].attrs["partition_boundaries"])
  self.assertEqual(after["throughput"].attrs["partition_attempts"]["train"],before["throughput"].attrs["partition_attempts"]["train"]-1)
 def test_equal_decision_timestamps_use_feasible_group_safe_cuts(self):
  frame=events(80);attempts=[f"run:a{i}" for i in range(46,66)];same=int(frame.loc[frame.attempt_id.eq("run:a55"),"decision_unix_nano"].iloc[0]);mask=frame.attempt_id.isin(attempts);offsets=frame.loc[mask,"observed_unix_nano"]-frame.loc[mask,"decision_unix_nano"];frame.loc[mask,"decision_unix_nano"]=same;frame.loc[mask,"observed_unix_nano"]=same+offsets
  _,tables=build_training_tables(frame);report=preflight_training_tables(tables)
  for name,table in tables.items():
   self.assertLessEqual(int(table.groupby("decision_unix_nano").partition.nunique().max()),1)
   self.assertTrue(all(report[name]["partition_attempts"][part]>={"train":20,"early_stop":5,"calibration":5,"final_test":5}[part] for part in ("train","early_stop","calibration","final_test")))
 def test_label_context_must_match_canonical_decision(self):
  for column,value,pattern in (("decision_unix_nano",1,"decision_unix_nano"),("transport","udp","transport"),("features",[9.]*8,"features")):
   frame=events(80);index=frame.index[frame.event_type.eq("outcome")][0];frame.at[index,column]=value
   with self.subTest(column=column),self.assertRaisesRegex(ValueError,pattern):build_training_tables(frame)
  frame=events(80);index=frame.index[frame.event_type.eq("outcome")][0];frame.at[index,"observed_unix_nano"]=int(frame.at[index,"decision_unix_nano"])-1
  with self.assertRaisesRegex(ValueError,"observed_unix_nano"):build_training_tables(frame)
 def test_conflicting_failure_outcomes_are_rejected(self):
  frame=events(80); row=frame.loc[frame.event_type.eq("outcome")].iloc[0].copy(); row["event_seq"]=int(frame.event_seq.max())+1; row["event_id"]="run:conflict"; row["outcome"]="success" if row["outcome"]=="failure" else "failure"; row["value"]=0. if row["outcome"]=="success" else 1.
  with self.assertRaisesRegex(ValueError,"冲突"):
   build_training_tables(pd.concat([frame,pd.DataFrame([row])],ignore_index=True))
class TrainingTests(unittest.TestCase):
 def train(self):
  attempts,tables=build_training_tables(events()); models={};meta={}
  for name in DIMENSIONS:
   models[name],meta[name]=train_dimension(name,tables[name]); self.assertGreater(models[name].best_iteration,0); self.assertLessEqual(meta[name]["confidence"],MAX_PRIOR_CONFIDENCE)
  return attempts,models,meta
 def test_weighted_median(self):self.assertEqual(_weighted_median(np.array([0.,10.,20.,30.]),np.array([1.,1.,10.,1.])),20.)
 def test_throughput_split_counts_independent_attempts(self):
  _,tables=build_training_tables(events(80)); table=tables["throughput"]; final=table[table.partition.eq("final_test")].iloc[[0]]; inflated=pd.concat([table[~table.partition.eq("final_test")],pd.concat([final]*20,ignore_index=True)],ignore_index=True)
  with self.assertRaisesRegex(ValueError,"独立 attempt"):train_dimension("throughput",inflated)
 def test_preflight_reports_all_insufficient_heads_before_training(self):
  attempts,tables=build_training_tables(events(80))
  for name in ("connect","throughput"):
   table=tables[name]; final=table.loc[table.partition.eq("final_test")].iloc[[0]].copy(); tables[name]=pd.concat([table.loc[~table.partition.eq("final_test")],final],ignore_index=True)
  with self.assertRaisesRegex(ValueError,r"connect.*throughput"):
   preflight_training_tables(tables)
 def test_failure_class_gate_thresholds_and_deficits(self):
  frame=events(120,failure_every=10)
  outcomes=frame.loc[frame.event_type.eq("outcome")].copy()
  _,initial=build_training_tables(frame)
  failure=initial["failure"]
  wanted={"train":8,"early_stop":8,"calibration":3,"final_test":3}
  positive=set()
  for part,count in wanted.items():positive.update(failure.loc[failure.partition.eq(part),"attempt_id"].drop_duplicates().iloc[:count])
  mask=frame.event_type.eq("outcome"); frame.loc[mask,"outcome"]=np.where(frame.loc[mask,"attempt_id"].isin(positive),"failure","success");frame.loc[mask,"value"]=np.where(frame.loc[mask,"attempt_id"].isin(positive),1.,0.)
  _,tables=build_training_tables(frame); report=preflight_training_tables(tables);gate=report["failure"]["class_gate"]
  self.assertTrue(gate["passed"]);self.assertEqual(gate["minimums"],FAILURE_MIN_CLASS_ATTEMPTS)
  victim=tables["failure"].loc[(tables["failure"].partition.eq("early_stop"))&tables["failure"].label.eq(1),"attempt_id"].iloc[0]
  frame.loc[mask&frame.attempt_id.eq(victim),["outcome","value"]]=["success",0.]
  _,tables=build_training_tables(frame);report=preflight_training_tables(tables);gate=report["failure"]["class_gate"]
  self.assertFalse(gate["passed"]);self.assertEqual(gate["deficits"],[{"partition":"early_stop","class":"failure","actual":7,"required":8,"missing":1}])
  _,meta=train_dimension("failure",tables["failure"],report["failure"]);self.assertEqual(meta["confidence"],0.);self.assertIn("early_stop",meta["_evidence"]["confidence_gate"])
 def test_four_models_native_manifest(self):
  attempts,models,meta=self.train()
  with tempfile.TemporaryDirectory() as tmp:
   output=Path(tmp)/"SagePrior.json"; manifest=save_bundle(output,attempts,models,meta); evidence=save_training_evidence(output.with_name("SagePrior.evidence.json"),attempts,meta,{"training_commit":"1"*40,"consumer_commit":"2"*40}); self.assertEqual(json.loads(output.read_text()),manifest); self.assertEqual(manifest["source_schema"],SOURCE_SCHEMA); self.assertEqual(manifest["feature_schema"],FEATURE_SCHEMA); self.assertEqual(set(manifest["dimensions"]),set(DIMENSIONS));self.assertEqual(evidence["algorithm_signature"],ALGORITHM_SIGNATURE)
   for name,d in manifest["dimensions"].items():self.assertTrue((output.parent/d["model"]).is_file());self.assertEqual(d["output_transform"],"sigmoid" if name=="failure" else "identity")
   self.assertNotIn("partition_boundaries",manifest["dimensions"]["failure"])
   self.assertEqual(set(evidence["dimensions"]),set(DIMENSIONS));self.assertIn("partition_boundaries",evidence["dimensions"]["throughput"]);self.assertIn("class_gate",evidence["dimensions"]["failure"])
 def test_detailed_training_log(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);data=root/"data";data.mkdir();write_ready(data,events());output=root/"SagePrior.json";log_path=root/"training_sage.log"
   run_training(["--data-dir",str(data),"--output",str(output),"--log-file",str(log_path)])
   content=log_path.read_text(encoding="utf-8")
   for marker in ("[步骤1]","[步骤2]","[步骤3] 四维训练前统一预检","decision_unix_nano","observed_unix_nano","candidate=","invalid/range dropped=","late dropped=","训练 connect 维度","训练 response 维度","训练 throughput 维度","训练 failure 维度","最佳迭代","最终验证","model_error=","baseline_error=","strategy=train weighted median","strategy=train weighted failure prevalence","confidence=","gate=","[步骤5]","training evidence:","训练完成"):self.assertIn(marker,content)
   self.assertRegex(content,r"manifest: .*sha256=[0-9a-f]{64}")
   for name in DIMENSIONS:self.assertRegex(content,rf"{name}.*sha256=[0-9a-f]{{64}}")

 def test_log_setup_failure_leaves_no_handler(self):
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(OSError):setup_logging(Path(tmp))
   self.assertEqual(LOGGER.handlers,[])
 def test_deterministic_and_atomic(self):
  bundles=[]; trained=[]
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   for generation in ("one","two"):
    attempts,models,meta=self.train(); output=root/generation/"SagePrior.json"; manifest=save_bundle(output,attempts,models,meta); payload={output.name:output.read_bytes()}
    for d in manifest["dimensions"].values():payload[d["model"]]=(output.parent/d["model"]).read_bytes()
    bundles.append(payload);trained.append((attempts,models,meta))
   self.assertEqual(bundles[0],bundles[1]); output=root/"atomic"/"SagePrior.json";output.parent.mkdir();output.write_text("old\n")
   class Broken:
    best_iteration=1
    def save_model(self,*_a,**_k):raise RuntimeError("injected")
   attempts,models,meta=trained[0]
   with self.assertRaisesRegex(RuntimeError,"injected"):save_bundle(output,attempts,{**models,"response":Broken()},meta)
   self.assertEqual(output.read_text(),"old\n")
 def test_fixed_fixture_model_bytes_and_algorithm_signature(self):
  fixture=Path(__file__).resolve().parents[1]/"tests"/"fixtures"/"sage_prior";expected=json.loads((fixture/"expected_hashes.json").read_text(encoding="utf-8"));frame=load_native_events(fixture);attempts,tables=build_training_tables(frame);preflight=preflight_training_tables(tables);models={};metadata={}
  self.assertLess(frame.loc[frame.event_type.eq("decision"),"decision_unix_nano"].nunique(),len(attempts));self.assertGreater(sum(table.attrs["late_dropped_rows"] for table in tables.values()),0)
  for name in DIMENSIONS:models[name],metadata[name]=train_dimension(name,tables[name],preflight[name])
  with tempfile.TemporaryDirectory() as tmp:
   output=Path(tmp)/"SagePrior.json";manifest=save_bundle(output,attempts,models,metadata);evidence_output=Path(tmp)/"SagePrior.evidence.json";save_training_evidence(evidence_output,attempts,metadata,{"training_commit":"1"*40,"consumer_commit":"2"*40});got={"algorithm_signature":ALGORITHM_SIGNATURE,"evidence_sha256":hashlib.sha256(evidence_output.read_bytes()).hexdigest(),"manifest_sha256":hashlib.sha256(output.read_bytes()).hexdigest(),"model_sha256":{name:manifest["dimensions"][name]["sha256"] for name in DIMENSIONS}}
  self.assertEqual(got,expected)
if __name__=="__main__":unittest.main()
