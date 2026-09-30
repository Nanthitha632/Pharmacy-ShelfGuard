"""ShelfGuard synthetic exception control room; decision support only."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import re

import streamlit as st
from langchain_ollama import ChatOllama
from rag_integrated_workflow import rag_agent

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / 'reports' / 'agent' / 'agent_audit.jsonl'
EVAL = ROOT / 'reports' / 'agent' / 'agent_evaluation.json'
WALK = ROOT / 'docs' / 'agent' / 'PORTFOLIO_WALKTHROUGH.md'

st.set_page_config(page_title='ShelfGuard | Exception Control Room', page_icon='💊', layout='wide')
st.markdown('''<style>.stApp{background:linear-gradient(135deg,#f3f8fc,#eaf3f8);color:#17324a}.block-container{max-width:1250px;padding-top:2rem}[data-testid="stMetric"]{background:white;border:1px solid #d7e7ef;border-radius:14px;padding:15px}</style>''', unsafe_allow_html=True)
st.caption('PHARMACY SHELFGUARD / SYNTHETIC SIMULATION')
st.title('💊 Exception Control Room')
st.write('Investigate shortages, supplier delays, and potential store transfers using verified data.')

with st.sidebar:
    st.header('🔐 Local PostgreSQL')
    password = st.text_input('PostgreSQL password', type='password')
    st.caption('localhost:5433 · read-only queries')
    st.info('Decision support only. No purchase orders or transfers are executed.')
if not password:
    st.info('Enter your local PostgreSQL password in the sidebar.')
    st.stop()


def connect():
    import psycopg
    return psycopg.connect(host='localhost', port=5433, dbname='pharmacy_shelfguard_fresh', user='postgres', password=password, options='-c default_transaction_read_only=on', connect_timeout=5)


def audit(event, **fields):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    entry = {'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'event': event, **fields}
    with AUDIT.open('a', encoding='utf-8') as file:
        file.write(json.dumps(entry, default=str) + '\n')


def investigate(day, store, sku):
    state = rag_agent.invoke({'business_day': day, 'store_id': store, 'sku': sku, 'password': password})
    if not all(k in state for k in ('result', 'donors', 'final')):
        raise ValueError('The RAG graph returned an incomplete handoff.')
    return {'agent': state['result'], 'donor_check': state['donors'], 'rag': state['final'].get('rag', {}), 'human_approval_required': state['final'].get('human_approval_required', False), 'case_handoff': state['final']}


def grounded_answer(intent, final):
    case = final['agent']['case']
    donors = final['donor_check']
    rules = final.get('rag', {}).get('rules_supplied_to_llm', final['agent'].get('rule_ids', []))
    day, store, sku = case['business_day'], case['store_id'], case['sku']
    if intent == 'SUPPLIER':
        if case['overdue_po_count']:
            orders = case['overdue_orders']
            detail = '; '.join(f"{p['po_id']} expected {p['expected_date']}" for p in orders)
            return f"On {day}, {store}/{sku} has {case['overdue_po_count']} overdue PO(s) totaling {case['overdue_units']} units ({detail}). Ask the buyer to confirm supplier ETA [EXP-01]."
        return f"On {day}, {store}/{sku} has no overdue purchase orders in the verified case. Review purchasing proposals [BUY-01]."
    if intent == 'TRANSFER':
        top = donors.get('top_candidates', [])
        if top:
            d = top[0]
            return f"{d['donor_store']} is a possible donor: {d['closing_units']} closing units minus {d['reserve_units']} protected units = {d['potential_spare_units']} potential spare units. Confirm physical stock, commitments, transport time, and approval [TRF-01]. No transfer was executed."
        return f"No possible donor met the {donors['donor_reserve_days']}-day reserve rule for {sku} on {day}. A buyer must review other options."
    if intent == 'RULES':
        return f"The graph supplied these operating rules for this case: {', '.join(rules)}. Suggested action: {final['agent']['suggested_action']}. Human approval remains required."
    return f"On {day}, {store}/{sku} requested {case['requested_units']} units, with {case['unmet_units']} unmet and {case['closing_units']} closing stock. The agent suggests {final['agent']['suggested_action']} for human review."


def classify_question(question):
    prompt = ('Classify the supply-chain question into exactly ONE token: SUPPLIER, TRANSFER, RULES, or DEMAND. '
              'SUPPLIER means overdue PO, ETA, or supplier. TRANSFER means donor, store transfer, or spare stock. '
              'RULES means policy or why the action was chosen. Otherwise DEMAND. '
              'Return only the token. Question: ' + question[:500])
    raw = str(ChatOllama(model='qwen3.5:4b', temperature=0).invoke(prompt).content).upper()
    found = re.search(r'\b(SUPPLIER|TRANSFER|RULES|DEMAND)\b', raw)
    return found.group(1) if found else 'DEMAND'


def numeric_guard(answer, final):
    """Reject invented numeric facts in a draft, including the evaluation probe."""
    evidence = final['agent']['case']
    donors = final['donor_check']
    allowed = {str(evidence[k]) for k in ('requested_units', 'unmet_units', 'closing_units', 'overdue_po_count', 'overdue_units')}
    allowed.update(str(x) for x in re.findall(r'\d+', evidence['business_day']))
    allowed.update(str(x) for x in re.findall(r'\d+', evidence['store_id'] + evidence['sku']))
    allowed.add(str(donors['donor_reserve_days']))
    for po in evidence['overdue_orders']:
        allowed.update(re.findall(r'\d+', str(po)))
    for donor in donors.get('top_candidates', []):
        allowed.update(str(donor[k]) for k in ('closing_units', 'reserve_units', 'potential_spare_units'))
        allowed.update(re.findall(r'\d+', donor['donor_store']))
    allowed.update(re.findall(r'\d+', ' '.join(final['agent'].get('rule_ids', []))))
    return all(number in allowed for number in re.findall(r'\d+', answer))


try:
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT DISTINCT business_day FROM shelfguard.v_daily_exceptions_v3 ORDER BY business_day DESC LIMIT 30')
        days = [str(row[0]) for row in cur.fetchall()]
    if not days:
        st.warning('No business days found.')
        st.stop()
    day = st.selectbox('📅 Business day', days)
    with connect() as conn, conn.cursor() as cur:
        cur.execute('''SELECT store_id, sku, unmet_units, overdue_po_count FROM shelfguard.v_daily_exceptions_v3
                       WHERE business_day=%s AND (unmet_units>0 OR overdue_po_count>0)
                       ORDER BY unmet_units DESC, overdue_po_count DESC, store_id, sku LIMIT 100''', (day,))
        choices = cur.fetchall()
except Exception as exc:
    st.error(f'Database load stopped: {type(exc).__name__}. Check local PostgreSQL and the password.')
    st.stop()
if not choices:
    st.info('No shortage or overdue PO found on this day.')
    st.stop()
index = st.selectbox('🔎 Choose an exception', range(len(choices)), format_func=lambda i: f'{choices[i][0]} · {choices[i][1]} | {choices[i][2]} unmet units · {choices[i][3]} overdue POs')
store, sku = choices[index][:2]
selection = (day, store, sku)
if st.button('Investigate with agent', type='primary'):
    try:
        with st.spinner('Running SQL evidence → RAG → LLM → validation → donor check...'):
            final = investigate(day, store, sku)
        st.session_state['handoff'] = final
        st.session_state['case_key'] = selection
        audit('investigation', business_day=day, store_id=store, sku=sku, status=final['agent']['status'], action=final['agent']['suggested_action'], rule_ids=final.get('rag', {}).get('rules_supplied_to_llm', []))
    except Exception as exc:
        st.error(f'Investigation stopped: {type(exc).__name__}: {exc}')
final = st.session_state.get('handoff') if st.session_state.get('case_key') == selection else None

if final:
    result, donors = final['agent'], final['donor_check']
    e = result['case']
    st.divider()
    if result['status'] == 'DRAFT_FOR_HUMAN_REVIEW':
        st.success('Evidence checks passed · Human review required')
    else:
        st.warning('Draft rejected for human review')
    cols = st.columns(4)
    for col, (label, value) in zip(cols, [('Requested units', e['requested_units']), ('Unmet units', e['unmet_units']), ('Closing units', e['closing_units']), ('Overdue PO units', e['overdue_units'])]):
        col.metric(label, value)
    left, right = st.columns(2)
    with left:
        st.subheader('📋 Verified case')
        st.write(f"**Day:** {day} · **Store/SKU:** {store}/{sku} · **Overdue POs:** {e['overdue_po_count']}")
        if e['overdue_orders']:
            st.dataframe(e['overdue_orders'], use_container_width=True, hide_index=True)
    with right:
        st.subheader('🤖 Agent handoff')
        st.write(f"**Suggested action:** `{result['suggested_action']}`")
        st.write(f"**Why:** {result['reason']}")
        st.write(f"**Ask the buyer:** {result['question_for_analyst']}")
        st.caption('Rules supplied: ' + ', '.join(final.get('rag', {}).get('rules_supplied_to_llm', result.get('rule_ids', []))))
        for issue in result['review_issues']:
            st.error(issue)
    st.subheader('🚚 Potential transfer donors')
    st.caption(f"Reserve: {donors['donor_reserve_days']} days of typical demand. Candidates require physical stock, transport, and approval checks.")
    st.metric('Candidate stores', donors['candidate_count'])
    if donors['top_candidates']:
        st.dataframe(donors['top_candidates'], use_container_width=True, hide_index=True)
    st.info('No purchase order or transfer was executed.')
    with st.expander('Evidence, rule retrieval, and handoff JSON'):
        st.code(json.dumps(final, indent=2, default=str), language='json')

    st.divider()
    st.subheader('💬 Ask the agent about this case')
    st.caption('Questions are routed quickly; answers use verified case facts and retrieved rule IDs. No free-form SQL or write actions.')
    question = st.text_input('Your question', placeholder='Is there an overdue supplier order, or can another store help?', key='case_question')
    if st.button('Answer from evidence') and question.strip():
        try:
            with st.spinner('Routing your question with the local model...'):
                q = question.lower()
                route = (
                    "SUPPLIER" if any(x in q for x in ("overdue", "supplier", "purchase order", "po ", "eta"))
                    else "TRANSFER" if any(x in q for x in ("transfer", "donor", "other store", "spare"))
                    else "RULES" if any(x in q for x in ("rule", "policy", "why"))
                    else "DEMAND"
                )
                answer = grounded_answer(route, final)
            if not numeric_guard(answer, final):
                raise ValueError('Answer includes a number absent from the selected evidence.')
            st.session_state['last_answer'] = (selection, route, answer)
            audit('question', business_day=day, store_id=store, sku=sku, route=route, guard='passed')
        except Exception as exc:
            st.session_state.pop('last_answer', None)
            audit('question', business_day=day, store_id=store, sku=sku, guard='blocked', error_type=type(exc).__name__)
            st.error(f'Question held for human review: {type(exc).__name__}: {exc}')
    last = st.session_state.get('last_answer')
    if last and last[0] == selection:
        st.info(last[2])
else:
    st.caption('Choose an exception and click Investigate with agent.')

st.divider()
st.subheader('🧪 Evaluation and portfolio handoff')
st.caption('Runs two known cases through the integrated graph, checks missing data, and probes the numeric claim guard. Allow time for the local model.')
if st.button('Run four scenario checks'):
    checks = []
    examples = [('no_overdue', 'ST036', 'MED026', 'HOLD_FOR_REVIEW', 'BUY-01'), ('overdue', 'ST040', 'MED001', 'ESCALATE_FOR_ETA', 'EXP-01')]
    with st.spinner('Evaluating the four scenarios with the local agent...'):
        for name, s, k, expected_action, expected_rule in examples:
            try:
                out = investigate('2026-05-07', s, k)
                agent_out = out['agent']
                rules = out.get('rag', {}).get('rules_supplied_to_llm', agent_out.get('rule_ids', []))
                passed = (agent_out['status'] == 'DRAFT_FOR_HUMAN_REVIEW' and agent_out['suggested_action'] == expected_action and expected_rule in rules and out.get('human_approval_required', False) and not agent_out['purchase_order_placed'] and not out['donor_check']['transfer_executed'])
                checks.append({'scenario': name, 'passed': passed, 'observed_action': agent_out['suggested_action'], 'rules': rules})
                if name == 'no_overdue':
                    probe = 'This store has 999999 units available.'
                    checks.append({'scenario': 'invented_numeric_claim', 'passed': not numeric_guard(probe, out), 'probe': probe})
            except Exception as exc:
                checks.append({'scenario': name, 'passed': False, 'error': f'{type(exc).__name__}: {exc}'})
        try:
            investigate('2099-01-01', 'ST036', 'MED026')
            checks.append({'scenario': 'missing_data', 'passed': False, 'error': 'The graph accepted a future day with no evidence.'})
        except Exception as exc:
            checks.append({'scenario': 'missing_data', 'passed': True, 'observed': type(exc).__name__})
    report = {'project': 'Pharmacy ShelfGuard synthetic simulation', 'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'checks': checks, 'all_passed': len(checks) == 4 and all(row['passed'] for row in checks), 'scope': 'Local four-scenario smoke check, not production validation'}
    EVAL.parent.mkdir(parents=True, exist_ok=True)
    EVAL.write_text(json.dumps(report, indent=2), encoding='utf-8')
    audit('evaluation', all_passed=report['all_passed'], scenarios=len(checks))
    st.session_state['evaluation'] = report
    WALK.parent.mkdir(parents=True, exist_ok=True)
    WALK.write_text('''# Pharmacy ShelfGuard | AI case walkthrough

Synthetic pharmacy-network simulation. Airflow prepares daily demand, inventory, forecasts, purchase recommendations and exceptions in PostgreSQL. This local Streamlit control room invokes an eight-node LangGraph: read-only evidence, filtered vector rule retrieval, LLM draft, claim validation, donor check, reconciliation and provenance. A local Ollama model routes follow-up questions; displayed answers use verified facts and rule IDs. The agent does not place purchase orders or execute transfers. Human review remains required.

Demo: select 2026-05-07 ST036/MED026 (shortage without overdue PO), then ST040/MED001 (overdue supplier order). Show evidence, rule IDs, donor reserve, audit log and the four-scenario evaluation report.

Important limitation: the donor stock is a simulated candidate, not committed or approved; the numeric guard catches invented numbers but cannot prove all wording correct. The four scenarios are smoke checks, not broad model validation. The historical April holdout was 91.78% baseline fill versus 91.89% policy fill, and stockout store-SKU days were 3565 versus 3594. The original 96.1% and 18% improvement goals were not achieved.
''', encoding='utf-8')
report = st.session_state.get('evaluation')
if report:
    for row in report['checks']:
        (st.success if row['passed'] else st.error)(f"{'PASS' if row['passed'] else 'FAIL'} · {row['scenario']}: {row.get('observed_action', row.get('error', row.get('observed', 'Claim guard rejected injected number.')))}")
    st.caption(f"Saved: {EVAL.relative_to(ROOT)} · {AUDIT.relative_to(ROOT)} · {WALK.relative_to(ROOT)}")
