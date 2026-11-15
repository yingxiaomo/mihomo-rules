import json, math, tempfile, unittest
from pathlib import Path
import numpy as np
import pandas as pd
from smart_trainer.train_sage_prior import DIMENSIONS, FEATURE_ORDER, FEATURE_SCHEMA, MAX_PRIOR_CONFIDENCE, SOURCE_SCHEMA, _weighted_median, build_features, build_training_tables, load_native_events, save_bundle, train_dimension

def feature(i): return [0.,1.,math.log1p(443),float(i%97),float(i%89),float(i%83),52.,3.]
def events(count=180):
 out=[]; seq=0; base=1_800_000_000_000_000_000
 for i in range(count):
  attempt=f"run:a{i+1}"; decision=base+i*10_000_000_000
  common={"source_schema":SOURCE_SCHEMA,"feature_schema":FEATURE_SCHEMA,"run_id":"run","attempt_id":attempt,"decision_unix_nano":decision,"group_id":"g","node_id":f"n{i%7}","node_id_stable":True,"transport":"tcp","scene":"default","features":feature(i)}
  failed=i%7==0
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
 def test_four_models_native_manifest(self):
  attempts,models,meta=self.train()
  with tempfile.TemporaryDirectory() as tmp:
   output=Path(tmp)/"SagePrior.json"; manifest=save_bundle(output,attempts,models,meta); self.assertEqual(json.loads(output.read_text()),manifest); self.assertEqual(manifest["source_schema"],SOURCE_SCHEMA); self.assertEqual(manifest["feature_schema"],FEATURE_SCHEMA); self.assertEqual(set(manifest["dimensions"]),set(DIMENSIONS))
   for name,d in manifest["dimensions"].items():self.assertTrue((output.parent/d["model"]).is_file());self.assertEqual(d["output_transform"],"sigmoid" if name=="failure" else "identity")
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
if __name__=="__main__":unittest.main()
