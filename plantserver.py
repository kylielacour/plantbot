#!/usr/bin/env python3
"""Plantbot editor + API server (runs on benedict).

The always-on source of truth for plant data. Serves:
  GET  /                 editable table of all plants + live computed schedule
  GET  /api/plants       raw plant config (JSON)          [Mac fetches this]
  POST /api/plants       overwrite plant config           [editor Save]
  GET  /api/state        last_watered per plant (JSON)    [Mac fetches this]
  POST /api/last_watered {plant_id, date}                 [Mac reports completions]
  GET  /api/schedule     computed interval + amount per plant (live climate + OPB)
  GET  /api/climate      current climate + source

Plant data lives in plants.yaml; last_watered in state/watering_state.json —
the same files the model already uses, so nothing else changes.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import yaml
from flask import Flask, jsonify, request

import climate
import plantstore
import watering_model
from openplantbook import OpenPlantbook
from units import ml_to_cups_str

BASE_DIR = Path(__file__).resolve().parent
PLANTS_PATH = Path(os.environ.get("PLANTBOT_PLANTS", BASE_DIR / "plants.yaml"))
LATITUDE = float(os.environ.get("LATITUDE", "40"))

app = Flask(__name__)


def _raw_plants() -> list[dict]:
    if not PLANTS_PATH.exists():
        return []
    data = yaml.safe_load(PLANTS_PATH.read_text()) or {}
    return data.get("plants", []) or []


@app.get("/api/plants")
def api_plants():
    return jsonify({"plants": _raw_plants()})


@app.post("/api/plants")
def api_save_plants():
    body = request.get_json(force=True, silent=True) or {}
    plants = body.get("plants")
    if not isinstance(plants, list):
        return jsonify({"error": "expected {'plants': [...]}"}), 400
    ids = []
    for p in plants:
        if not isinstance(p, dict) or not p.get("id"):
            return jsonify({"error": "every plant needs an id"}), 400
        ids.append(p["id"])
    if len(ids) != len(set(ids)):
        return jsonify({"error": "duplicate plant ids"}), 400
    plantstore.save_plants_raw(plants, PLANTS_PATH)
    return jsonify({"ok": True, "count": len(plants)})


@app.get("/api/state")
def api_state():
    return jsonify(plantstore.WateringState()._data)


@app.post("/api/last_watered")
def api_last_watered():
    body = request.get_json(force=True, silent=True) or {}
    pid, date_s = body.get("plant_id"), body.get("date")
    if not pid or not date_s:
        return jsonify({"error": "need plant_id and date"}), 400
    try:
        date = dt.date.fromisoformat(date_s[:10])
    except ValueError:
        return jsonify({"error": "bad date"}), 400
    plantstore.WateringState().set_last_watered(pid, date)
    return jsonify({"ok": True})


def _climate():
    source, desc = climate.from_env()
    return source.conditions(), desc


@app.get("/api/climate")
def api_climate():
    cond, desc = _climate()
    return jsonify({"source": desc, "temp_c": round(cond.temp_c, 1),
                    "humidity_pct": round(cond.humidity_pct), "lux": cond.lux})


@app.get("/api/schedule")
def api_schedule():
    today = dt.date.today()
    entries = plantstore.load_plants(PLANTS_PATH)
    state = plantstore.WateringState()
    opb = OpenPlantbook.from_env()
    cond, _ = _climate()

    out = []
    for entry in entries:
        plant = entry.plant
        base = opb.cached_species_data(entry.pid) if (opb and entry.pid) else None
        species = entry.species_with_overrides(base)
        last = state.get_last_watered(plant.id)
        rec = watering_model.watering_recommendation(
            plant, species, cond, last, today, LATITUDE)
        out.append({
            "id": plant.id, "name": plant.name,
            "interval_days": rec.interval_days,
            "next_date": rec.next_date.isoformat(),
            "due_in": (rec.next_date - today).days,
            "amount_ml": round(rec.amount_ml),
            "amount_str": ml_to_cups_str(rec.amount_ml),
            "explanation": rec.explanation,
            "warnings": rec.warnings,
            "last_watered": last.isoformat() if last else None,
        })
    return jsonify(out)


@app.get("/")
def index():
    return PAGE


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Plantbot</title>
<style>
:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:1rem;max-width:1100px;margin:auto;line-height:1.4}
h1{font-size:1.4rem;font-weight:600}
#climate{color:#666;font-size:.9rem;margin-bottom:1rem}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th,td{border-bottom:1px solid #8883;padding:6px 5px;text-align:left;vertical-align:top}
th{font-weight:600;position:sticky;top:0;background:Canvas}
input,select{font:inherit;padding:4px 6px;border:1px solid #8886;border-radius:6px;background:Field;color:FieldText;width:100%;box-sizing:border-box}
input[type=checkbox]{width:auto}
td.calc{white-space:nowrap;color:#2a7}
td.warn{color:#c73}
.sched{font-weight:600}
.wrap{overflow-x:auto}
button{font:inherit;padding:8px 16px;border:1px solid #8886;border-radius:8px;background:Field;color:FieldText;cursor:pointer}
button.primary{background:#2a7;color:#fff;border-color:#2a7}
.bar{position:sticky;bottom:0;background:Canvas;padding:12px 0;display:flex;gap:10px;align-items:center;border-top:1px solid #8883;margin-top:8px}
#status{color:#666;font-size:.9rem}
.narrow{width:78px}.mid{width:120px}
button.tune{padding:4px 9px;font-size:.8rem;white-space:nowrap}
.overlay{position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center;z-index:10;padding:12px}
.panel{background:Canvas;color:CanvasText;border:1px solid #8886;border-radius:12px;padding:16px;max-width:460px;width:100%;max-height:92vh;overflow:auto}
.phead{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.phead b{font-size:1.1rem}
.mgrid label{display:block;font-size:.82rem;color:#666;margin:11px 0 3px}
.mgrid input[type=range]{width:100%}
.mgrid select{width:100%}
.mgrid hr{border:none;border-top:1px solid #8883;margin:14px 0 2px}
.mout{display:flex;gap:12px;margin:14px 0 6px}
.metric{flex:1;background:#8881;border-radius:8px;padding:10px}
.metric small{color:#666}.metric b{font-size:1.4rem;display:block}
.fbar{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:.78rem}
.fbar .nm{width:78px;color:#666}
.fbar .tr{flex:1;height:6px;background:#8882;border-radius:4px;overflow:hidden}
.fbar .fl{height:100%;background:#2a7}.fbar .vl{width:42px;text-align:right}
.hint{font-size:.8rem;color:#666;margin:8px 0 0}
.mbtns{display:flex;gap:10px;margin-top:14px}
</style></head><body>
<h1>🌱 Plantbot</h1>
<div id=climate>loading climate…</div>
<div class=wrap><table id=tbl><thead><tr>
<th>Name</th><th>Species (pid)</th><th class=narrow>Soil ml</th><th class=mid>Soil</th>
<th class=narrow>Lux</th><th class=mid>Water use</th><th class=mid>Growth</th><th class=narrow>Drain</th>
<th>When</th><th>Amount</th><th></th></tr></thead><tbody id=rows></tbody></table></div>
<div class=bar>
  <button class=primary id=save>Save changes</button>
  <button id=add>Add plant</button>
  <span id=status>Save to update the schedule</span>
</div>
<div id=modal class=overlay style="display:none"><div class=panel>
  <div class=phead><b id=m-title></b><button id=m-close>✕</button></div>
  <div class=mgrid>
    <label>Soil volume — <span id=m-volout></span></label><input type=range id=m-vol min=100 max=25000 step=50>
    <label>Soil type</label><select id=m-soil><option>standard</option><option>peat</option><option>coco</option><option>aroid</option><option>cactus</option><option>moisture</option></select>
    <label>Light — <span id=m-luxout></span> lux (<span id=m-luxword></span>)</label><input type=range id=m-lux min=0 max=1000 step=1>
    <label>Water use</label><select id=m-wu><option>low</option><option>medium</option><option>high</option></select>
    <label>Growth</label><select id=m-grow><option>auto</option><option>active</option><option>dormant</option></select>
    <label><input type=checkbox id=m-drain> has drainage hole</label>
    <hr><div class=hint>Conditions below are a preview — the saved schedule uses live climate.</div>
    <label>Temp — <span id=m-tempout></span> °F</label><input type=range id=m-temp min=50 max=95 step=1>
    <label>Humidity — <span id=m-humout></span>%</label><input type=range id=m-hum min=10 max=90 step=1>
    <label>Month — <span id=m-monthout></span></label><input type=range id=m-month min=1 max=12 step=1>
  </div>
  <div class=mout>
    <div class=metric><small>Water every</small><b><span id=m-interval></span> days</b></div>
    <div class=metric><small>Give it</small><b id=m-amount></b></div>
  </div>
  <div id=m-factors></div>
  <p class=hint id=m-explain></p>
  <div class=mbtns><button class=primary id=m-apply>Apply</button><button id=m-cancel>Cancel</button></div>
</div></div>
<script>
var SOILS=["standard","peat","coco","aroid","cactus","moisture"];
var WU=["low","medium","high"];
var GROW=["auto","active","dormant"];
var plants=[],sched={};
var climF=72,climHum=50,curMonth=new Date().getMonth()+1,mId=null;
function $(id){return document.getElementById(id);}
function opt(v,list){return list.map(function(x){return '<option'+(x===v?' selected':'')+'>'+x+'</option>';}).join('');}
function esc(s){return (s==null?'':String(s)).replace(/"/g,'&quot;');}
function row(p){
  var s=sched[p.id]||{};
  var when=s.interval_days?('every '+s.interval_days+'d'+(s.due_in<=0?' — due now':' ('+s.due_in+'d)')):'—';
  var warn=(s.warnings&&s.warnings.length)?'<div class=warn>⚠ '+s.warnings.join('; ')+'</div>':'';
  return '<tr data-id="'+esc(p.id)+'">'
   +'<td><input class=f-name value="'+esc(p.name)+'"></td>'
   +'<td><input class=f-pid value="'+esc(p.pid)+'" placeholder="(none)"></td>'
   +'<td><input class="f-vol narrow" type=number value="'+esc(p.soil_volume_ml)+'"></td>'
   +'<td><select class="f-soil mid">'+opt(p.soil_type||"standard",SOILS)+'</select></td>'
   +'<td><input class="f-lux narrow" type=number value="'+esc(p.light_lux)+'"></td>'
   +'<td><select class="f-wu mid">'+opt(p.water_use||"medium",WU)+'</select></td>'
   +'<td><select class="f-grow mid">'+opt(p.growth_state||"auto",GROW)+'</select></td>'
   +'<td><input class=f-drain type=checkbox '+(p.has_drainage!==false?'checked':'')+'></td>'
   +'<td class="calc sched">'+when+warn+'</td>'
   +'<td class=calc>'+(s.amount_str||'—')+'</td>'
   +'<td><button type=button class=tune>⚙ Tune</button></td></tr>';
}
function render(){document.getElementById('rows').innerHTML=plants.map(row).join('');}
function collect(){
  var out=[];
  document.querySelectorAll('#rows tr').forEach(function(tr){
    var g=function(c){return tr.querySelector(c);};
    var pid=g('.f-pid').value.trim();
    var lux=g('.f-lux').value.trim();
    out.push({id:tr.getAttribute('data-id'),name:g('.f-name').value.trim(),
      pid:pid||null,soil_volume_ml:parseFloat(g('.f-vol').value)||null,
      soil_type:g('.f-soil').value,light_lux:lux?parseFloat(lux):null,
      water_use:g('.f-wu').value,growth_state:g('.f-grow').value,
      has_drainage:g('.f-drain').checked});
  });
  return out;
}
function setStatus(t){document.getElementById('status').textContent=t;}
function loadSchedule(){
  return fetch('api/schedule').then(function(r){return r.json();}).then(function(list){
    sched={};list.forEach(function(s){sched[s.id]=s;});render();});
}
function loadClimate(){fetch('api/climate').then(function(r){return r.json();}).then(function(c){
  var tf=Math.round(c.temp_c*9/5+32);climF=tf;climHum=Math.round(c.humidity_pct);
  document.getElementById('climate').textContent='Climate: '+c.source+' — '+tf+'°F / '+c.humidity_pct+'% RH';});}
function loadAll(){fetch('api/plants').then(function(r){return r.json();}).then(function(d){
  plants=d.plants||[];render();loadSchedule();loadClimate();});}
document.getElementById('save').onclick=function(){
  setStatus('saving…');
  fetch('api/plants',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({plants:collect()})}).then(function(r){return r.json();}).then(function(res){
    if(res.error){setStatus('error: '+res.error);return;}
    setStatus('saved '+res.count+' plants');loadSchedule();});
};
document.getElementById('add').onclick=function(){
  var id=prompt('Short id for the new plant (e.g. new-fern):');if(!id)return;
  plants=collect();plants.push({id:id,name:id,pid:null,soil_volume_ml:1000,soil_type:'standard',
    light_lux:4000,water_use:'medium',growth_state:'auto',has_drainage:true});render();
  setStatus('added — remember to Save');
};

var AWC={standard:.35,peat:.38,coco:.40,aroid:.28,cactus:.22,moisture:.45},
    MAD={low:.9,medium:.5,high:.35},KC={low:.3,medium:1,high:1.45},
    ET=0.0215,CAP=0.08,ND=0.8,LAT=40,MLCUP=236.588,MLTB=14.7868,
    MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function svp(t){return 0.6108*Math.exp(17.27*t/(t+237.3));}
var VREF=svp(21)*0.5;
function clamp(x,a,b){return Math.max(a,Math.min(b,x));}
function luxF(l){return clamp(0.55*Math.log10(Math.max(l,1))-1.05,0.5,1.7);}
function dayLen(d){var dec=23.45*Math.sin(2*Math.PI*(284+d)/365)*Math.PI/180,p=LAT*Math.PI/180,c=-Math.tan(p)*Math.tan(dec);if(c>=1)return 0;if(c<=-1)return 24;return 2*Math.acos(c)*180/Math.PI/15;}
function luxWord(l){return l<2500?'dim':l<7000?'moderate':l<20000?'bright indirect':'direct sun';}
function luxFromSlider(v){return Math.round(Math.pow(10,2+(v/1000)*3));}
function sliderFromLux(l){return Math.round((Math.log10(Math.max(l,100))-2)/3*1000);}
function cups(ml){if(ml<0.25*MLCUP){var t=ml/MLTB;return t<1?Math.round(ml)+' ml':Math.round(t)+' tbsp';}
  var F=[[0,''],[.25,'¼'],[1/3,'⅓'],[.5,'½'],[2/3,'⅔'],[.75,'¾'],[1,'']],c=ml/MLCUP,w=Math.floor(c),r=c-w,b=F[0];
  F.forEach(function(f){if(Math.abs(r-f[0])<Math.abs(r-b[0]))b=f;});var fv=b[0],fs=b[1];
  if(fv>=0.99){w++;fs='';}if(w===0&&fs)return fs+' cup';if(w>0&&fs)return w+fs+' cups';if(w>0)return w===1?'1 cup':w+' cups';return '0 cups';}
function fbar(nm,m){var w=clamp(m/2*100,0,100);return '<div class=fbar><span class=nm>'+nm+'</span><span class=tr><span class=fl style="width:'+w.toFixed(0)+'%"></span></span><span class=vl>'+m.toFixed(2)+'×</span></div>';}
function computeTune(){
  var vol=+$('m-vol').value,soil=$('m-soil').value,lux=luxFromSlider(+$('m-lux').value),
      wu=$('m-wu').value,grow=$('m-grow').value,drain=$('m-drain').checked,
      tF=+$('m-temp').value,hum=+$('m-hum').value,mo=+$('m-month').value,tC=(tF-32)*5/9,doy=Math.round((mo-0.5)*30.42);
  $('m-volout').textContent=vol>=1000?(vol/1000).toFixed(1)+' L':vol+' ml';
  $('m-luxout').textContent=lux;$('m-luxword').textContent=luxWord(lux);
  $('m-tempout').textContent=tF;$('m-humout').textContent=hum;$('m-monthout').textContent=MON[mo-1];
  var fv=clamp(svp(tC)*(1-hum/100)/VREF,0.4,2.5),fl=luxF(lux),L=dayLen(doy),
      fs=clamp(0.5+0.5*(L/12),0.5,1.4),fg=grow==='active'?1:grow==='dormant'?0.4:clamp(0.4+0.6*(L-9)/5,0.4,1),kc=KC[wu]||1;
  var dep=vol*(AWC[soil]||.35)*(MAD[wu]||.5),amt=Math.min(dep,CAP*vol)*(drain?1:ND),
      loss=Math.max(ET*vol*fv*fl*fs*fg*kc,0.1),iv=Math.round(clamp(dep/loss,2,30));
  $('m-interval').textContent=iv;$('m-amount').textContent=cups(amt);
  $('m-factors').innerHTML=fbar('dryness',fv)+fbar('light',fl)+fbar('season',fs)+fbar('growth',fg)+fbar('water-use',kc);
  $('m-explain').textContent='~'+Math.round(loss)+' ml/day lost · refill '+Math.round(amt)+' ml';
}
function openTune(id){
  plants=collect();var p=plants.filter(function(x){return x.id===id;})[0];if(!p)return;mId=id;
  $('m-title').textContent=p.name;$('m-vol').value=p.soil_volume_ml||1000;$('m-soil').value=p.soil_type||'standard';
  $('m-lux').value=sliderFromLux(p.light_lux||4000);$('m-wu').value=p.water_use||'medium';
  $('m-grow').value=p.growth_state||'auto';$('m-drain').checked=p.has_drainage!==false;
  $('m-temp').value=climF;$('m-hum').value=climHum;$('m-month').value=curMonth;
  computeTune();$('modal').style.display='flex';
}
function closeTune(){$('modal').style.display='none';}
function applyTune(){
  var p=plants.filter(function(x){return x.id===mId;})[0];if(p){
    p.soil_volume_ml=+$('m-vol').value;p.soil_type=$('m-soil').value;p.light_lux=luxFromSlider(+$('m-lux').value);
    p.water_use=$('m-wu').value;p.growth_state=$('m-grow').value;p.has_drainage=$('m-drain').checked;
    render();setStatus('updated '+p.name+' — Save to persist');}
  closeTune();
}
document.getElementById('rows').addEventListener('click',function(e){
  if(e.target.closest('.tune')){var tr=e.target.closest('tr');if(tr)openTune(tr.getAttribute('data-id'));}});
['m-vol','m-soil','m-lux','m-wu','m-grow','m-drain','m-temp','m-hum','m-month'].forEach(function(id){
  $(id).addEventListener('input',computeTune);});
$('m-apply').onclick=applyTune;$('m-cancel').onclick=closeTune;$('m-close').onclick=closeTune;
$('modal').addEventListener('click',function(e){if(e.target===$('modal'))closeTune();});

loadAll();
</script></body></html>"""


if __name__ == "__main__":
    host = os.environ.get("PLANTBOT_HOST", "127.0.0.1")
    port = int(os.environ.get("PLANTBOT_PORT", "8770"))
    try:
        from waitress import serve
        print(f"plantserver on http://{host}:{port}")
        serve(app, host=host, port=port)
    except ImportError:
        app.run(host=host, port=port)
