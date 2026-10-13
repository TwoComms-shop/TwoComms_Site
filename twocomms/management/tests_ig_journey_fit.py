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
