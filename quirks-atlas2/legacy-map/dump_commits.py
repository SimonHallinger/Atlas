import json,sys
H=json.load(open('data/history.json'));C=H['commits']
for n in sys.argv[1].split(','):
    f=H['functions'][n]
    hs=list(dict.fromkeys(f.get('log_L',[])[::-1]+[b['hash'] for b in f['blame']]))
    hs=sorted([h for h in hs if h in C],key=lambda h:C[h]['date'])
    print(f"##### {n} ({len(hs)} commits)")
    for h in hs:
        c=C[h]; m=c['message'].split('\n',1)[1].strip() if '\n' in c['message'] else ''
        print(f"-- {c['short']} {c['date']} {c['author']} | {c['subject']}")
        if m: print("   "+m[:int(sys.argv[2])].replace('\n','\n   '))
        ex=[k+':'+str(c[k]) for k in ('fixes','links','bug_refs','cc_stable','reported_by') if c[k]]
        if ex: print("   ["+' ; '.join(ex)[:400]+"]")
