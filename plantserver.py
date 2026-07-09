#!/usr/bin/env python3
"""Plantbot editor + API server (runs on benedict).

The always-on source of truth for plant data. Serves:
  GET  /                 card editor + live computed schedule
  GET  /api/plants       raw plant config (JSON)          [Mac fetches this]
  POST /api/plants       overwrite plant config           [editor Save]
  GET  /api/state        last_watered per plant (JSON)    [Mac fetches this]
  POST /api/last_watered {plant_id, date}                 [Mac reports completions]
  GET  /api/schedule     computed interval + amount + ideal needs per plant
  GET  /api/climate      current climate + source

Plant data lives in plants.yaml; last_watered in state/watering_state.json.
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
        band = watering_model.sun_band(plant.sun)
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
            "water_use": plant.water_use,
            "sun": plant.sun,
            "current_lux": round(plant.light_lux) if plant.light_lux is not None else None,
            "ideal_lux": ({"min": band[0], "ideal": band[1], "max": band[2]} if band else None),
        })
    return jsonify(out)


@app.get("/")
def index():
    return PAGE


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Plantbot</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='88'>🪴</text></svg>">
<style>
:root{--card:#fff;--ink:#1a1c1a;--muted:#70746e;--line:#e7e7e2;--accent:#2f9e6f;--accent-soft:#e9f6ef;--warn:#b06a25;--warn-soft:#f7efe1;--danger:#bd4438;--danger-soft:#fbeae7;--radius:14px}
@media (prefers-color-scheme:dark){:root{--card:#1b1d1b;--ink:#e8eae6;--muted:#969b93;--line:#31352e;--accent:#5cc999;--accent-soft:#1d3a2d;--warn:#e0a061;--warn-soft:#33291b;--danger:#e2776d;--danger-soft:#38231f}}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;color:var(--ink);-webkit-font-smoothing:antialiased}
.wrap{max-width:1100px;margin:0 auto;padding:22px 16px 48px}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.addbtn{flex:none;width:40px;height:40px;border-radius:50%;border:1px solid var(--line);background:var(--card);color:var(--accent);font-size:1.7rem;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center}
.addbtn:hover{border-color:var(--accent);background:var(--accent-soft)}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%) translateY(16px);background:var(--ink);color:var(--card);padding:9px 16px;border-radius:20px;font-size:.85rem;opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;z-index:30}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
h1{font-size:1.5rem;font-weight:650;margin:0;letter-spacing:-.02em}
.climate{color:var(--muted);font-size:.9rem;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;margin-top:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:15px 16px;display:flex;flex-direction:column;gap:9px}
.chead{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.card h2{font-size:1.05rem;margin:0;font-weight:600;letter-spacing:-.01em}
.sched{font-size:1.05rem;font-weight:600}
.sched .amt{color:var(--accent)}
.meta{color:var(--muted);font-size:.8rem}
.due{font-size:.7rem;font-weight:600;padding:2px 9px;border-radius:20px;background:var(--accent-soft);color:var(--accent);white-space:nowrap}
.due.now{background:var(--danger-soft);color:var(--danger)}
.pills{display:flex;gap:6px;flex-wrap:wrap}
.pill{font-size:.72rem;padding:2px 9px;border-radius:20px;background:var(--accent-soft);color:var(--accent);white-space:nowrap}
.pill.warn{background:var(--warn-soft);color:var(--warn)}
.needs{border-top:1px solid var(--line);padding-top:10px;display:flex;flex-direction:column;gap:8px}
.lightrow{font-size:.76rem;color:var(--muted);display:flex;justify-content:space-between;gap:8px}
.track{position:relative;height:8px;border-radius:6px;background:var(--line)}
.zone{position:absolute;top:0;bottom:0;background:var(--accent-soft);border-radius:6px}
.mark{position:absolute;top:-3px;width:3px;height:14px;border-radius:2px;background:var(--accent);transform:translateX(-1px)}
.mark.dim{background:var(--danger)}.mark.bright{background:var(--warn)}
.nolight{font-size:.76rem;color:var(--muted)}
.edit{align-self:flex-start;margin-top:2px;font:inherit;font-size:.82rem;padding:6px 14px;border:1px solid var(--line);border-radius:9px;background:transparent;color:var(--ink);cursor:pointer}
.edit:hover{border-color:var(--accent);color:var(--accent)}
button.btn{font:inherit;padding:9px 16px;border:1px solid var(--line);border-radius:10px;background:transparent;color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
#status{color:var(--muted);font-size:.85rem}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:20;padding:14px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;max-width:460px;width:100%;max-height:92vh;overflow:auto}
.phead{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.phead b{font-size:1.15rem}
.x{border:none;background:transparent;color:var(--muted);font-size:1.2rem;cursor:pointer}
.mgrid label{display:block;font-size:.8rem;color:var(--muted);margin:11px 0 3px}
.mgrid input[type=range]{width:100%}
.mgrid input[type=number],.mgrid select{width:100%;font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink)}
.mgrid .hint{font-size:.74rem;color:var(--muted);margin-top:8px}
.mout{display:flex;gap:10px;margin:15px 0 4px}
.metric{flex:1;background:var(--accent-soft);border-radius:11px;padding:11px 12px}
.metric small{color:var(--muted);font-size:.72rem}.metric b{display:block;font-size:1.35rem;margin-top:1px}
.fbar{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:.76rem}
.fbar .nm{width:76px;color:var(--muted)}
.fbar .tr{flex:1;height:6px;background:var(--line);border-radius:4px;overflow:hidden}
.fbar .fl{height:100%;background:var(--accent)}.fbar .vl{width:42px;text-align:right}
.mbtns{display:flex;gap:10px;margin-top:14px}
</style></head><body>
<div class=wrap>
  <header>
    <div><h1>🪴 Plantbot</h1><div class=climate id=climate>loading…</div></div>
    <button class=addbtn id=add title="Add a plant" aria-label="Add a plant">+</button>
  </header>
  <div class=grid id=grid></div>
</div>
<div id=toast class=toast></div>

<div id=modal class=overlay style="display:none"><div class=panel>
  <div class=phead><b id=m-title></b><button class=x id=m-close>&times;</button></div>
  <div class=mgrid>
    <label>Name</label><input type=text id=m-name>
    <label>Soil volume — <span id=m-volout></span></label><input type=range id=m-vol min=100 max=25000 step=50>
    <label>Soil type</label><select id=m-soil></select>
    <label>Measured light — <span id=m-luxout></span> lux (<span id=m-luxword></span>)</label><input type=range id=m-lux min=0 max=1000 step=1>
    <label>Sun requirement</label><select id=m-sun></select>
    <label>Water preference</label><select id=m-wu></select>
    <label>Growth</label><select id=m-grow></select>
    <label><input type=checkbox id=m-drain style="width:auto"> has a drainage hole</label>
  </div>
  <div class=mout>
    <div class=metric><small>Water every</small><b><span id=m-interval></span> days</b></div>
    <div class=metric><small>Give it</small><b id=m-amount></b></div>
  </div>
  <div id=m-factors></div>
  <div class=mbtns><button class="btn primary" id=m-apply>Apply</button><button class="btn" id=m-cancel>Cancel</button></div>
</div></div>

<script>
var SOIL=["standard","peat","coco","aroid","cactus","moisture"];
var GROW=["auto","active","dormant"];
var WU=[["dry","Dry"],["dry_mesic","Dry-Mesic"],["mesic","Mesic"],["wet_mesic","Wet-Mesic"],["wet","Wet"]];
var SUN=[["","— not set —"],["full_sun","Full sun"],["sun_to_part_shade","Sun – part shade"],["part_shade","Part / dappled shade"],["part_to_full_shade","Part – full shade"],["full_shade","Full shade"]];
var WU_ALIAS={low:"dry",medium:"mesic",high:"wet"};
var AWC={standard:.35,peat:.38,coco:.40,aroid:.28,cactus:.22,moisture:.45};
var KC={dry:.30,dry_mesic:.55,mesic:1.0,wet_mesic:1.25,wet:1.45,low:.30,medium:1.0,high:1.45};
var MAD={dry:.90,dry_mesic:.70,mesic:.50,wet_mesic:.40,wet:.30,low:.90,medium:.50,high:.30};
var POUR={dry:.06,dry_mesic:.07,mesic:.08,wet_mesic:.09,wet:.10,low:.06,medium:.08,high:.10};
var ET=0.0215,ND=0.8,LAT=40,MLCUP=236.588,MLTB=14.7868;
var MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
var plants=[],sched={},climF=72,climHum=50,curMonth=new Date().getMonth()+1,mId=null;
function $(id){return document.getElementById(id);}
function esc(s){return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
function clamp(x,a,b){return Math.max(a,Math.min(b,x));}
function svp(t){return 0.6108*Math.exp(17.27*t/(t+237.3));}
var VREF=svp(21)*0.5;
function luxF(l){return clamp(0.55*Math.log10(Math.max(l,1))-1.05,0.5,1.7);}
function dayLen(d){var dec=23.45*Math.sin(2*Math.PI*(284+d)/365)*Math.PI/180,p=LAT*Math.PI/180,c=-Math.tan(p)*Math.tan(dec);if(c>=1)return 0;if(c<=-1)return 24;return 2*Math.acos(c)*180/Math.PI/15;}
function luxWord(l){return l<2500?'dim':l<7000?'moderate':l<20000?'bright indirect':'direct sun';}
function luxFromSlider(v){return Math.round(Math.pow(10,2+(v/1000)*3));}
function sliderFromLux(l){return Math.round((Math.log10(Math.max(l||4000,100))-2)/3*1000);}
function luxPos(l){return clamp((Math.log10(Math.max(l||1,50))-2)/3,0,1)*100;}
function normWU(v){v=(v||'mesic');return WU_ALIAS[v]||v;}
function fmtLux(l){return l>=1000?Math.round(l/1000)+'k':l;}
function cups(ml){if(ml<0.25*MLCUP){var t=ml/MLTB;return t<1?Math.round(ml)+' ml':Math.round(t)+' tbsp';}
  var F=[[0,''],[.25,'¼'],[1/3,'⅓'],[.5,'½'],[2/3,'⅔'],[.75,'¾'],[1,'']],c=ml/MLCUP,w=Math.floor(c),r=c-w,b=F[0];
  F.forEach(function(f){if(Math.abs(r-f[0])<Math.abs(r-b[0]))b=f;});var fv=b[0],fs=b[1];
  if(fv>=0.99){w++;fs='';}if(w===0&&fs)return fs+' cup';if(w>0&&fs)return w+fs+' cups';if(w>0)return w===1?'1 cup':w+' cups';return '0 cups';}
function label(list,key){for(var i=0;i<list.length;i++)if(list[i][0]===key)return list[i][1];return key;}

function lightBar(cur,ideal){
  if(!ideal) return '<div class=nolight>Sun requirement not set</div>';
  var status=cur==null?'':(cur<ideal.min?'dim':(cur>ideal.max?'bright':''));
  var mark=cur==null?'':'<span class="mark '+status+'" style="left:'+luxPos(cur)+'%"></span>';
  return '<div class=track><span class=zone style="left:'+luxPos(ideal.min)+'%;width:'+(luxPos(ideal.max)-luxPos(ideal.min))+'%"></span>'+mark+'</div>';
}
function card(p){
  var s=sched[p.id]||{};
  var due=s.due_in<=0?'<span class="due now">due now</span>':'<span class=due>in '+s.due_in+'d</span>';
  var warns=(s.warnings||[]).map(function(w){return '<span class="pill warn">⚠ '+esc(w)+'</span>';}).join('');
  var idl=s.ideal_lux;
  var lr=idl?('current '+(s.current_lux!=null?fmtLux(s.current_lux):'—')+' · ideal '+fmtLux(idl.min)+'–'+fmtLux(idl.max)+' lux'):'no ideal set';
  var needPills='<span class=pill>💧 '+esc(label(WU,normWU(s.water_use)))+'</span>'+(s.sun?'<span class=pill>☀ '+esc(label(SUN,s.sun))+'</span>':'');
  return '<div class=card><div class=chead><h2>'+esc(p.name)+'</h2>'+(s.interval_days?due:'')+'</div>'
    +'<div class=sched>💧 every '+(s.interval_days||'—')+' days · <span class=amt>'+(s.amount_str||'—')+'</span></div>'
    +'<div class=meta>last watered '+(s.last_watered||'never')+'</div>'
    +(warns?'<div class=pills>'+warns+'</div>':'')
    +'<div class=needs><div class=lightrow><span>Light</span><span>'+lr+'</span></div>'+lightBar(s.current_lux,idl)+'<div class=pills>'+needPills+'</div></div>'
    +'<button class=edit data-id="'+esc(p.id)+'">Adjust</button></div>';
}
function render(){$('grid').innerHTML=plants.map(card).join('');}
function selOpts(list){return list.map(function(o){var k=Array.isArray(o)?o[0]:o,t=Array.isArray(o)?o[1]:o;return '<option value="'+esc(k)+'">'+esc(t)+'</option>';}).join('');}

function loadSchedule(){return fetch('api/schedule').then(function(r){return r.json();}).then(function(list){sched={};list.forEach(function(s){sched[s.id]=s;});render();});}
function loadClimate(){fetch('api/climate').then(function(r){return r.json();}).then(function(c){
  var tf=Math.round(c.temp_c*9/5+32);climF=tf;climHum=Math.round(c.humidity_pct);
  $('climate').textContent='Climate: '+c.source+' — '+tf+'°F / '+c.humidity_pct+'% RH';});}
function loadAll(){fetch('api/plants').then(function(r){return r.json();}).then(function(d){plants=d.plants||[];render();loadSchedule();loadClimate();});}

$('add').onclick=function(){var name=prompt('Name of the new plant:');if(!name)return;
  var id=name.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'')||('plant-'+Date.now());
  if(plants.some(function(x){return x.id===id;}))id=id+'-'+Date.now();
  plants.push({id:id,name:name,soil_volume_ml:1000,soil_type:'standard',sun:'part_shade',water_use:'mesic',growth_state:'auto',has_drainage:true});
  render();persist('Added '+name);openTune(id);};

$('m-soil').innerHTML=selOpts(SOIL);$('m-grow').innerHTML=selOpts(GROW);$('m-wu').innerHTML=selOpts(WU);$('m-sun').innerHTML=selOpts(SUN);

function fbar(nm,m){var w=clamp(m/2*100,0,100);return '<div class=fbar><span class=nm>'+nm+'</span><span class=tr><span class=fl style="width:'+w.toFixed(0)+'%"></span></span><span class=vl>'+m.toFixed(2)+'×</span></div>';}
function computeTune(){
  var vol=+$('m-vol').value,soil=$('m-soil').value,lux=luxFromSlider(+$('m-lux').value),wu=$('m-wu').value,grow=$('m-grow').value,drain=$('m-drain').checked;
  var tC=(climF-32)*5/9,hum=climHum,doy=Math.round((curMonth-0.5)*30.42);
  $('m-volout').textContent=vol>=1000?(vol/1000).toFixed(1)+' L':vol+' ml';
  $('m-luxout').textContent=lux;$('m-luxword').textContent=luxWord(lux);
  var fv=clamp(svp(tC)*(1-hum/100)/VREF,0.4,2.5),fl=luxF(lux),L=dayLen(doy),
      fs=clamp(0.5+0.5*(L/12),0.5,1.4),fg=grow==='active'?1:grow==='dormant'?0.4:clamp(0.4+0.6*(L-9)/5,0.4,1),kc=KC[wu]||1;
  var dep=vol*(AWC[soil]||.35)*(MAD[wu]||.5),amt=(POUR[wu]||.08)*vol*(drain?1:ND),
      loss=Math.max(ET*vol*fv*fl*fs*fg*kc,0.1),iv=Math.round(clamp(dep/loss,2,30));
  $('m-interval').textContent=iv;$('m-amount').textContent=cups(amt);
  $('m-factors').innerHTML=fbar('dryness',fv)+fbar('light',fl)+fbar('season',fs)+fbar('growth',fg)+fbar('water-use',kc);
}
function openTune(id){var p=plants.filter(function(x){return x.id===id;})[0];if(!p)return;mId=id;
  $('m-title').textContent=p.name||id;$('m-name').value=p.name||'';
  $('m-vol').value=p.soil_volume_ml||1000;$('m-soil').value=p.soil_type||'standard';
  $('m-lux').value=sliderFromLux(p.light_lux||4000);$('m-sun').value=p.sun||'';$('m-wu').value=normWU(p.water_use);
  $('m-grow').value=p.growth_state||'auto';$('m-drain').checked=p.has_drainage!==false;
  computeTune();$('modal').style.display='flex';document.body.style.overflow='hidden';}
function closeTune(){$('modal').style.display='none';document.body.style.overflow='';}
function toast(msg){var t=$('toast');t.textContent=msg;t.classList.add('show');clearTimeout(window.__tt);window.__tt=setTimeout(function(){t.classList.remove('show');},1800);}
function persist(msg){fetch('api/plants',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plants:plants})})
  .then(function(r){return r.json();}).then(function(res){toast(res.error?('error: '+res.error):(msg||'Saved'));loadSchedule();});}
function applyTune(){var p=plants.filter(function(x){return x.id===mId;})[0];if(p){
    p.name=$('m-name').value.trim()||p.id;
    p.soil_volume_ml=+$('m-vol').value;p.soil_type=$('m-soil').value;p.light_lux=luxFromSlider(+$('m-lux').value);
    p.sun=$('m-sun').value||null;p.water_use=$('m-wu').value;p.growth_state=$('m-grow').value;p.has_drainage=$('m-drain').checked;
    render();persist('Saved '+p.name);}closeTune();}

$('grid').addEventListener('click',function(e){var b=e.target.closest('.edit');if(b)openTune(b.getAttribute('data-id'));});
['m-vol','m-soil','m-lux','m-wu','m-grow','m-drain'].forEach(function(id){$(id).addEventListener('input',computeTune);});
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
