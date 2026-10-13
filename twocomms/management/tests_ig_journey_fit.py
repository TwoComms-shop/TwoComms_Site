from pathlib import Path
import shutil
import subprocess

from django.test import SimpleTestCase


class JourneyFitTests(SimpleTestCase):
    def test_all_and_aftercare_maps_fit_both_dimensions_and_resize_without_scrolling(self):
        source = (Path(__file__).parent / "static/management/ig_journey.js").read_text().replace(
            "window.TwcJourney={create:options=>new Journey(options)};",
            "window.TwcJourney={Journey};")
        program = "global.window={};\n" + source + r'''
const assert=require('node:assert/strict');
const compact=window.TwcJourney.Journey.prototype.compactActualGeometry({positions:new Map([
  ['start',{col:0,row:1}],['payment',{col:1,row:1}],['received',{col:2,row:1}],
  ['history',{col:3,row:0}],['completed',{col:3,row:1}]
])},390);
assert.equal(compact.positions.size,5);assert.ok(compact.width<=366);assert.ok(compact.height<=450);
assert.equal(new Set([...compact.positions.values()].map(p=>p.col+':'+p.row)).size,5);
for(const p of compact.positions.values()){assert.ok(p.x>=50&&p.x<=compact.width-50);assert.ok(p.y+70<=compact.height);}
const centred=Object.create(window.TwcJourney.Journey.prototype);
Object.assign(centred,{modal:{},mapMode:'actual',mapWidth:512,mapHeight:230,zoom:1,
  map:{style:{}},canvas:{style:{}},zoomLabel:{},viewport:{clientWidth:1500,clientHeight:900,scrollLeft:0,scrollTop:0}});
centred.fitMap();assert.equal(centred.zoom,1.4);assert.ok(parseFloat(centred.map.style.left)>300);
for(const mode of ['actual','short','all']){
  const j=Object.create(window.TwcJourney.Journey.prototype);
  Object.assign(j,{modal:{},mapMode:mode,possibleFamily:'after',mapWidth:1200,mapHeight:1800,
    zoom:1,map:{style:{}},canvas:{style:{}},zoomLabel:{},
    viewport:{clientWidth:900,clientHeight:600,scrollLeft:70,scrollTop:500}});
  j.fitMap();
  assert.ok(j.mapWidth*j.zoom<=j.viewport.clientWidth-24);
  assert.ok(j.mapHeight*j.zoom<=j.viewport.clientHeight-24);
  assert.equal(j.viewport.scrollTop,0);assert.equal(j.viewport.scrollLeft,0);
  assert.equal(j.autoFit,true);assert.ok(parseFloat(j.map.style.left)>=12);
  j.viewport.clientWidth=390;j.viewport.clientHeight=450;j.fitMap();
  assert.ok(j.mapWidth*j.zoom<=366);assert.ok(j.mapHeight*j.zoom<=426);
  j.setZoom(.8);assert.equal(j.autoFit,false);
}
'''
        result = subprocess.run([shutil.which("node"), "-e", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
