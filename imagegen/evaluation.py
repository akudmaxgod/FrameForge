"""Offline quality metrics. Pending and uncertain decisions cannot count as passes."""
from collections import Counter

STATES={'pass','fail','uncertain','error','pending'}

def quality_metrics(records, decisions, *, split='holdout'):
    keyed={(r['frame'],r['label']):r for r in records}
    if len(keyed)!=len(records):raise ValueError('Duplicate evaluation sample')
    results={}
    for result in decisions:
        key=(result['frame'],result['label'])
        if key not in keyed or key in results:raise ValueError('Unknown or duplicate evaluation result')
        if result.get('sha256')!=keyed[key]['sha256']:raise ValueError('Evaluation image changed')
        if result.get('status') not in STATES:raise ValueError('Invalid evaluation status')
        results[key]=result['status']
    selected=[r for r in records if r['split']==split]
    counts=Counter();bad=corrected=detected=rejected=bad_uncertain=corrected_uncertain=0
    for record in selected:
        status=results.get((record['frame'],record['label']),'pending');counts[status]+=1
        if record['label']=='bad':
            bad+=1;detected+=status=='fail'
            bad_uncertain+=status=='uncertain'
        elif record['label']=='corrected':
            corrected+=1;rejected+=status=='fail'
            corrected_uncertain+=status=='uncertain'
    complete=bool(selected) and not (counts['pending'] or counts['error'] or counts['uncertain'])
    recall=detected/bad if bad else None
    rejection=rejected/corrected if corrected else None
    return {'split':split,'samples':len(selected),'states':dict(counts),
        'known_bad_count':bad,'corrected_count':corrected,
        'known_bad_detection_rate':recall,'corrected_rejection_rate':rejection,
        'known_bad_uncertain_count':bad_uncertain,'corrected_uncertain_count':corrected_uncertain,
        'known_bad_not_accepted_rate':(detected+bad_uncertain)/bad if bad else None,
        'corrected_automatic_acceptance_rate':sum(results.get((r['frame'],r['label']))=='pass' for r in selected if r['label']=='corrected')/corrected if corrected else None,
        'complete':complete,
        'thresholds_met_on_labelled_pairs':complete and recall is not None and recall>=.9 and rejection is not None and rejection<=.05,
        'independent_good_frame_false_rejection_rate':None,
        'manual_intervention_rate_new_project':None,
        'quality_acceptance_met':False,
        'limitations':['Corrected images are prior audit examples, not independently labelled ordinary good frames.',
            'New scenario evaluation and real quota/cost measurements are still required.']}
