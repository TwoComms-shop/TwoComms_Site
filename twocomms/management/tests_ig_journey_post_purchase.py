from copy import deepcopy
from types import SimpleNamespace
from django.test import SimpleTestCase
from management.services.ig_journey_post_purchase import append_post_purchase_context, KEYS
from management.services.ig_journey_consent import consent_progress


def graph(step=4, order=7, **progress):
    return {'nodes': [{'id': f'client-order:{order}', 'semantic_key': 'client_order_context',
        'state': 'complete' if step == 4 else 'partial', 'episode_id': None, 'scope': 'client',
        'fulfillment_progress': {'step': step, 'evidence_refs': [{'kind': 'order', 'id': order}], **progress}}],
        'edges': [], 'marketing_consent': consent_progress()}


class AfterPurchaseTests(SimpleTestCase):
    def test_received_opens_entire_connected_tail_without_completing_actions(self):
        source = graph(); before = deepcopy(source)
        result = append_post_purchase_context(source)
        self.assertEqual(source, before)
        tail = [n for n in result['nodes'] if n.get('post_purchase')]
        self.assertEqual({n['semantic_key'] for n in tail}, set(KEYS))
        self.assertTrue(all(n['state'] is None and not n['current'] for n in tail))
        reached = {'client-order:7'}
        for _ in result['nodes']:
            reached.update(e['to_node_id'] for e in result['edges'] if e['from_node_id'] in reached)
        self.assertEqual(reached, {n['id'] for n in result['nodes']})
        self.assertTrue(all(e['relation'] == 'route' and not e['evidence_refs'] for e in result['edges']))
        consent = next(n for n in tail if n['semantic_key'] == 'channel_consent')['consent_progress']
        self.assertEqual(consent['delivery']['status'], 'received')
        self.assertEqual(consent['permission']['status'], 'unconfirmed')
        self.assertEqual(append_post_purchase_context(result), result)

    def test_shipped_enables_service_but_later_steps_wait_for_receipt(self):
        result = append_post_purchase_context(graph(2))
        gates = {n['semantic_key']: n['post_purchase'] for n in result['nodes'] if n.get('post_purchase')}
        self.assertEqual(gates['post_sale_case']['readiness'], 'available')
        self.assertEqual(gates['post_purchase_contact_offer']['readiness'], 'waiting')
        self.assertEqual(gates['reward_delivery']['readiness'], 'waiting')

    def test_no_unlock_for_cancelled_unverified_or_historical(self):
        for source in (graph(0,cancelled=True), graph(1), graph(4,cancelled=True)):
            self.assertEqual(append_post_purchase_context(source), source)
        pending=append_post_purchase_context(graph(1,completion_unverified=True))
        self.assertEqual(next(n for n in pending['nodes'] if n['semantic_key']=='post_purchase_contact_offer')['post_purchase']['readiness'],'waiting')
        source = graph()
        self.assertEqual(append_post_purchase_context(source,is_history=True),source)

    def test_multiple_orders_and_new_purchase_do_not_share_outcomes(self):
        source = graph(); source['nodes'] += graph(2,order=8)['nodes']
        source['nodes'].append({'id':'new-purchase','semantic_key':'new_purchase_interest','episode_id':99,'state':'open'})
        result = append_post_purchase_context(source)
        self.assertEqual(next(n for n in result['nodes'] if n['id']=='new-purchase')['state'],'open')
        gates = [n for n in result['nodes'] if n.get('post_purchase')]
        self.assertEqual(len(gates),len(KEYS)*2)
        by_id = {n['id']:n for n in gates}
        for e in result['edges']:
            if e['from_node_id'] in by_id and e['to_node_id'] in by_id:
                self.assertEqual(by_id[e['from_node_id']]['post_purchase']['order_id'],by_id[e['to_node_id']]['post_purchase']['order_id'])

    def test_opt_out_blocks_marketing_not_customer_service(self):
        source = graph();source['marketing_consent']=consent_progress(opted_out_at='now',opt_out_message_id=12)
        result=append_post_purchase_context(source)
        by_key={n['semantic_key']:n for n in result['nodes']}
        self.assertEqual(by_key['post_purchase_contact_offer']['post_purchase']['readiness'],'blocked')
        self.assertEqual(by_key['post_sale_case']['post_purchase']['readiness'],'available')

    def test_existing_bound_events_are_preserved(self):
        source=graph();source['nodes'][0].update(semantic_key='fulfillment',episode_id=9)
        fact={'id':'reward','semantic_key':'reward_delivery','episode_id':9,'state':'complete','evidence_refs':[{'kind':'message','id':55}]}
        source['nodes'].append(fact)
        result=append_post_purchase_context(source)
        reward=next(n for n in result['nodes'] if n['id']=='reward')
        self.assertEqual(reward['state'],'complete');self.assertEqual(reward['evidence_refs'],fact['evidence_refs'])
        self.assertEqual(len([n for n in result['nodes'] if n['semantic_key']=='reward_delivery']),1)

    def test_renderer_connects_full_tail_and_isolates_short_order_view(self):
        import json, subprocess, shutil
        from pathlib import Path
        from management.services.ig_journey_catalogue import journey_catalogue
        base=Path(__file__).parent/'static/management'
        js=(base/'ig_journey.js').read_text().replace('window.TwcJourney={create:options=>new Journey(options)};', 'window.TwcJourney={Journey,witnessed};')
        source=graph();source['nodes']+=graph(2,order=8)['nodes']
        source['nodes'].append({'id':'client-order:7:contact','semantic_key':'client_order_contact','scope':'client','episode_id':None,'producer':'client_order_assignments','contextual_binding':{'order_id':7},'consent_progress':consent_progress()})
        source['edges'].append({'id':'existing-contact','from_node_id':'client-order:7','to_node_id':'client-order:7:contact','relation':'client_order_lifecycle','evidence_refs':[]})
        data=append_post_purchase_context(source)
        program='global.window={};\n'+(base/'ig_journey_geometry.js').read_text()+js+'\nconst source='+json.dumps(data)+';const snapshot='+json.dumps({'catalogue':journey_catalogue()})+r'''
const assert=require('node:assert/strict');const {Journey,witnessed}=window.TwcJourney;
const j=Object.create(Journey.prototype);j.modal={};j.mapMode='short';j.possibleFamily='after';j.showPossible=true;j.afterPurchaseOrderId='post-purchase:7:post_sale_case';
const short=j.presentEvents(j.presentGraph(source,snapshot));
assert.ok(short.nodes.every(n=>n.id==='client-order:7'||n.post_purchase?.order_id===7));
assert.equal(short.nodes.length,10); // permission check is inside the opt-in composite
assert.ok(short.edges.every(e=>!witnessed(e)));
const reachable=new Set(['client-order:7']);for(let i=0;i<short.nodes.length;i++)for(const e of short.edges)if(reachable.has(e.from_node_id))reachable.add(e.to_node_id);
assert.equal(reachable.size,short.nodes.length);
for(const width of [320,390,900]){const g=window.TwcJourneyGeometry.aftercare({nodes:short.nodes,width});const ps=[...g.positions.values()];for(let a=0;a<ps.length;a++)for(let b=a+1;b<ps.length;b++)assert.ok(Math.abs(ps[a].x-ps[b].x)>=104||Math.abs(ps[a].y-ps[b].y)>=100);}
j.mapMode='all';const all=j.presentGraph(source,snapshot);
for(const id of ['post-purchase:7:reward_delivery','post-purchase:8:reward_delivery'])assert.ok(all.nodes.some(n=>n.id===id));
// The original snapshot is immutable and the same conditions survive every view.
assert.equal(source.nodes.find(n=>n.id==='post-purchase:7:post_sale_case').post_purchase.readiness,'available');
'''
        result=subprocess.run([shutil.which('node'),'-e',program],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
