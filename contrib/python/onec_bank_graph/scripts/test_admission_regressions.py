"""Invented boundary, monetary and format/row identity regressions."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
import archive_adapter as a
import statement_graph as sg
from test_archive_adapter import document, line, row


def parsed(body, edit=None):
    doc = document(body)
    if edit:
        edit(doc)
    st, en, rel, qa = a.parse_statement(doc, 1, 'synthetic')
    return {'dataset_id':'synthetic', 'statements':[st], 'entities':en, 'relations':rel}, qa


def diagnostic(reviewed):
    # Tests of source diagnostics deliberately exercise missing identifiers.
    return sg.build(reviewed, diagnostic=True)


class NineClaimTests(unittest.TestCase):
    def test_c1_repeated_full_header_is_decoration(self):
        doc = document([row('1', debit='2'), 'Exact purpose', row('2', credit='3'), 'Second'])
        header = copy.deepcopy(doc['pages'][0]['lines'][:10])
        doc['pages'][1]['lines'] = header + doc['pages'][1]['lines']
        st, _, _, qa = a.parse_statement(doc, 1, 'synthetic')
        self.assertEqual([o['properties']['purpose_raw'] for o in st['operations']], ['Exact purpose', 'Second'])
        self.assertFalse(any('continuation boundary' in g for x in qa['partial_rows'] for g in x['gaps']))

    def test_c2_unreadable_component_retains_visible_unit(self):
        for label in ('net', 'gross', 'комиссия:'):
            for unit in ('USD','BYR','Р УБ.'):
                with self.subTest(label=label,unit=unit):
                    purpose = label + ' [неразборчиво] ' + unit
                    cs = a.amount_components(purpose, 'inflow', '985')
                    self.assertEqual(cs[0][2], '[неразборчиво]')
                    self.assertEqual(a.component_unit(purpose, cs[0][0], cs[0][3])[0], unit)
                    self.assertIsNone(a.component_money(cs[0][2]))

    def test_c3_missing_document_number_preserves_movement(self):
        for number in ('', '?', '[?]'):
            r, _ = parsed([row('1', debit='2'), 'First', row(number, credit='3'), 'Second'])
            ops = r['statements'][0]['operations']
            self.assertEqual([o['properties']['amount'] for o in ops], ['2','3'])
            self.assertEqual([o['properties']['purpose_raw'] for o in ops], ['First','Second'])
            self.assertIsNone(ops[1]['properties']['document_number_raw'])

    def test_c4_purpose_is_not_closing_control(self):
        doc = document([row('1', debit='2'), 'Исходящее сальдо 123.45'])
        doc['pages'][1]['lines'] = [x for x in doc['pages'][1]['lines'] if not x['raw_text'].startswith(('ОБОРОТЫ','Исходящее сальдо 1.00'))]
        with self.assertRaises(ValueError):
            a.parse_statement(doc, 1, 'synthetic')

    def test_c5_reference_only_totals_do_not_promise_fee(self):
        for text in ('общая сумма 1000', 'Сумма операций: 1000','на сумму 1000'):
            cs = a.amount_components(text, 'inflow', '985')
            self.assertEqual(a.component_gaps(cs, 'inflow','985',text), [])

    def test_c6_basis_whole_token_and_units(self):
        for suffix in ('.??','?','abc',',55','.22'):
            cs = a.fee_basis('с суммы оборотов 760000.00'+suffix)
            self.assertTrue(cs)
            self.assertIsNone(a.component_money(cs[0][2]), suffix)
        cs = a.fee_basis('с суммы оборотов 760000.00 USD; ставка 1%')
        self.assertEqual(a.component_money(cs[0][2]),'760000.00')
        self.assertEqual(a.component_unit('с суммы оборотов 760000.00 USD; ставка 1%',cs[0][0],cs[0][3])[0],'USD')

    def test_c7_partial_cannot_erase_proven_bank_movement(self):
        r, _ = parsed([row('?',credit='3'),'Purpose'])
        g = diagnostic(r)
        p = next(n for n in g['nodes'] if n['type']=='Operation')['properties']
        p.update(amount=None,direction='unknown')
        self.assertFalse(sg.validate(g)['valid'])

    def test_c8_unknown_purpose_cannot_classify_fee(self):
        def edit(doc):doc['pages'][0]['lines'][11]['overlaps']=[20]
        r, _ = parsed([row('1',debit='2'),'Вознагр за прием налич денег'],edit)
        p = r['statements'][0]['operations'][0]['properties']
        self.assertEqual(p['purpose_status'],'unknown')
        self.assertIsNone(p['classification'])
        self.assertEqual(p['classification_status'],'unknown')

    def test_c9_new_fields_require_purpose_status(self):
        for retained in ('payment1c_comment_raw','settlement'):
            r, _ = parsed([row('1',credit='985'),'Удержана комиссия 15'])
            g = diagnostic(r)
            p = next(n for n in g['nodes'] if n['type']=='Operation')['properties']
            del p['purpose_status']
            if retained == 'payment1c_comment_raw':del p['settlement']
            else:del p['payment1c_comment_raw']
            self.assertFalse(sg.validate(g)['valid'])




def identity_body(direction='inflow', number='1', name='SYNTHETIC COUNTERPARTY', payer='111222333', payee='444555666'):
    return [row(number,credit='3' if direction=='inflow' else '',debit='2' if direction=='outflow' else ''),
            'Наименование корреспондента: '+name,
            'УНП плательщика: '+payer+' УНП получателя: '+payee+' УНП за кого: ', 'Exact purpose']


class IdentityAdmissionTests(unittest.TestCase):
    def test_source_observed_role_and_full_name_required_matrix(self):
        for direction in ('inflow','outflow'):
            for defect in ('none','name_absent','name_unreadable','unp_absent','unp_unreadable','wrong_role_unp',
                           'duplicate_unp','conflicting_unp','duplicate_name','name_overlap','unp_overlap','unresolved_continuation','zeros','short_unp','damaged_unp','whitespace','behalf_substitution'):
                with self.subTest(direction=direction,defect=defect):
                    body = identity_body(direction)
                    if defect=='name_absent':body.pop(1)
                    if defect=='name_unreadable':body[1]='Наименование корреспондента: ???'
                    if defect=='whitespace':body[1]='Наименование корреспондента:    '
                    if defect in ('zeros','short_unp','damaged_unp'):
                        value={'zeros':'000000000','short_unp':'123','damaged_unp':'11122233?'}[defect]
                        body[2]=body[2].replace('111222333' if direction=='inflow' else '444555666',value)
                    if defect=='behalf_substitution':
                        body[2]=body[2].replace('111222333' if direction=='inflow' else '444555666','')+'999888777'
                    if defect in ('unp_absent','unp_unreadable','wrong_role_unp'):
                        raw='' if defect!='unp_unreadable' else '???'
                        body[2]='УНП плательщика: '+(raw if direction=='inflow' else '111222333')+' УНП получателя: '+(raw if direction=='outflow' else '444555666')+' УНП за кого: '
                    if defect in ('duplicate_unp','conflicting_unp'):body.insert(3,body[2].replace('111222333','999888777') if defect=='conflicting_unp' else body[2])
                    if defect=='duplicate_name':body.insert(2,'Наименование корреспондента: SECOND NAME')
                    def edit(doc):
                        if defect=='name_overlap':doc['pages'][0]['lines'][11]['overlaps']=[28]
                        if defect=='unp_overlap':doc['pages'][0]['lines'][12]['overlaps']=[19]
                        if defect=='unresolved_continuation':doc['reviewed_continuations']=[]
                    reviewed, qa = parsed(body,edit)
                    graph=diagnostic(reviewed)
                    admission=sg.validate(graph)['processing_admission']
                    expected = 'admitted' if defect == 'none' else ('blocked' if qa['identity_admission']['field_types']['missing'] else 'partial')
                    self.assertEqual(admission['status'],expected)
                    self.assertEqual(qa['identity_admission']['status'],admission['status'])
                    if defect=='none':
                        self.assertTrue(sg.validate(sg.build(reviewed))['valid'])
                        cp=reviewed['statements'][0]['operations'][0]['properties']['counterparty']
                        self.assertEqual(cp['role'],'payer' if direction=='inflow' else 'payee')
                        self.assertEqual(cp['unp_raw'],'111222333' if direction=='inflow' else '444555666')
                    else:
                        if expected == 'blocked':
                            with self.assertRaises(ValueError):sg.build(reviewed)
                        else:
                            self.assertTrue(sg.validate(sg.build(reviewed))['valid'])
                        self.assertIsNone(qa['rows'][0]['document_view']['bank_document_kind_candidate'])

    def test_one_bad_row_holds_only_that_row_and_retains_every_entrypoint(self):
        for direction in ('inflow', 'outflow'):
            for bad_position in (0, 1):
                bodies = [identity_body('inflow', '1'), identity_body('outflow', '2')]
                bodies[bad_position] = identity_body(direction, str(bad_position+1), name='???')
                reviewed, qa = parsed(bodies[0]+bodies[1]); graph = sg.build(reviewed)
                admission = sg.identity_admission(graph)
                self.assertEqual(admission['status'], 'partial')
                self.assertEqual(admission['statements'][0]['clarification_count'], 1)
                self.assertEqual(admission['statements'][0]['ready_count'], 1)
                self.assertEqual(len(qa['rows']), 2)
                for i, r in enumerate(qa['rows']):
                    self.assertTrue(r['document_view']['statement_processing_allowed'])
                    self.assertEqual(r['document_view']['row_processing_allowed'], i != bad_position)
                    self.assertEqual(r['document_view']['processing']['status'], 'needs_clarification' if i == bad_position else 'ready')
                self.assertEqual(len([n for n in sg.combine([graph])['nodes'] if n['type']=='Operation']), 2)
                self.assertTrue(sg.validate(sg.annotate_rko(graph, {}))['valid'])
                held = admission['statements'][0]['rows'][bad_position]
                self.assertTrue(held['questions']); self.assertIn('Документ', held['questions'][0])
                with tempfile.TemporaryDirectory() as tmp:
                    geom=Path(tmp)/'geom.json';geom.write_text(json.dumps(document(bodies[0]+bodies[1])))
                    accepted, coverage=a.assemble([geom], 'synthetic')
                    self.assertEqual(len(accepted['statements'][0]['operations']),2)
                    self.assertEqual(coverage['statements'][0]['identity_admission']['status'],'partial')

    def test_legacy_and_manually_spoofed_identity_cannot_bypass(self):
        reviewed,_=parsed(identity_body());valid=sg.build(reviewed)
        for defect in ('legacy','strip_parties','strip_role_edges','wrong_role','header_holder','partial_identity',
                       'unobserved','missing_observation','wrong_observation','duplicate_observation','multiple_role_edges','orphan'):
            with self.subTest(defect=defect):
                graph=copy.deepcopy(valid);op=next(n for n in graph['nodes'] if n['type']=='Operation');cp=op['properties']['counterparty'];party=next(n for n in graph['nodes'] if n['id']==cp['party_id'])
                if defect=='legacy':del op['properties']['counterparty']
                if defect=='multiple_role_edges':graph['edges'].append(copy.deepcopy(next(e for e in graph['edges'] if e['type']=='PAYER')))
                if defect=='orphan':graph['nodes']=[n for n in graph['nodes'] if n['type']!='Statement']
                if defect=='strip_parties':graph['nodes']=[n for n in graph['nodes'] if n['type']!='Party']
                if defect=='strip_role_edges':graph['edges']=[e for e in graph['edges'] if e['type'] not in ('PAYER','PAYEE')]
                if defect=='wrong_role':cp['role']='payee'
                if defect=='header_holder':cp['party_id']=next(e['to'] for e in graph['edges'] if e['type']=='OWNED_BY')
                if defect=='partial_identity':cp['unp_raw']=None
                if defect=='unobserved':cp['status']='unknown'
                if defect=='missing_observation':party['observations']=[]
                if defect=='wrong_observation':party['observations'][0]['normalized']='999888777'
                if defect=='duplicate_observation':party['observations'].append(copy.deepcopy(party['observations'][0]))
                self.assertIn(sg.identity_admission(graph)['status'], ('blocked','partial'))
                with self.assertRaises(ValueError):sg.require_processing_admission(graph)

    def test_name_continuation_and_multipage_preserved(self):
        body=identity_body();body.insert(2,'                             FULL SERVICE UNIT')
        reviewed,qa=parsed(body);g=sg.build(reviewed)
        cp=reviewed['statements'][0]['operations'][0]['properties']['counterparty']
        self.assertEqual(cp['name_raw'],'SYNTHETIC COUNTERPARTY\n                             FULL SERVICE UNIT')
        self.assertEqual(sg.validate(g)['processing_admission']['status'],'admitted')
        self.assertEqual(qa['rows'][0]['document_view']['payment1c_comment_raw'],'Exact purpose')

    def test_empty_and_independently_observed_same_holder_literal(self):
        doc=document([],empty=True)
        st,en,rels,qa=a.parse_statement(doc,1,'synthetic')
        with self.assertRaises(ValueError):
            sg.build({'dataset_id':'synthetic','statements':[st],'entities':en,'relations':rels})
        self.assertEqual(qa['identity_admission']['field_types']['status'],'unproven')
        self.assertEqual(qa['operations'],0)
        for direction in ('inflow','outflow'):
            r,_=parsed(identity_body(direction,name='SYNTHETIC HOLDER'))
            self.assertEqual(sg.identity_admission(sg.build(r))['status'],'admitted')

    def test_component_mutation_matrix_and_damaged_operation_codes(self):
        for code in ('     ',' ?   ',' [?] ',' 6?  '):
            damaged=row('2',credit='3');damaged=damaged[:6]+code+damaged[11:]
            reviewed,_=parsed([row('1',debit='2'),'First',damaged,'Second'])
            ops=reviewed['statements'][0]['operations']
            self.assertEqual([o['properties']['amount'] for o in ops],['2','3'])
            self.assertIsNone(ops[1]['properties']['operation_code_raw'])
        for purpose in ('Вознагр за прием налич денег с суммы оборотов 1000 USD','Удержана комиссия 15 BYR'):
            body=identity_body('outflow' if 'оборотов' in purpose else 'inflow');body[-1]=purpose
            reviewed,_=parsed(body);g=sg.build(reviewed)
            self.assertTrue(sg.validate(g)['valid'])
            for key,value in [('value','999'),('value_raw','999'),('unit_raw','EUR'),('currency_raw','EUR'),('inclusion','excluded')]:
                bad=copy.deepcopy(g);component=next(n for n in bad['nodes'] if n['type']=='AmountComponent');component['properties'][key]=value
                self.assertFalse(sg.validate(bad)['valid'],(purpose,key))



class IdentityGuardTests(unittest.TestCase):
    def assert_blocked(self, body, edit=None):
        # Historical helper name: only the uncertain rows are now held.
        reviewed, qa = parsed(body, edit)
        graph = diagnostic(reviewed)
        status = qa['identity_admission']['status']
        self.assertIn(status, ('partial', 'blocked'))
        self.assertEqual(sg.identity_admission(graph)['status'], status)
        if status == 'blocked':
            with self.assertRaises(ValueError): sg.build(reviewed)
        else:
            accepted = sg.build(reviewed)
            self.assertTrue(sg.validate(accepted)['valid'])
            for row in qa['rows']:
                held = row['document_view']['counterparty']['status'] != 'observed'
                self.assertEqual(row['document_view']['processing']['status'], 'needs_clarification' if held else 'ready')
                if held: self.assertIsNone(row['document_view']['bank_document_kind_candidate'])
        return reviewed, qa

    def test_unreadable_name_anatomy_and_row_admission_matrix(self):
        damaged = ('�', 'SYNTHETIC � COMPANY', '---', '— –', '□', '■', '???', '[?]', '***',
                   'нечитаемо', '[НЕЧИТАЕМО]', 'неразборчиво', 'unknown', 'unreadable', 'нет данных',
                   'NULL', 'None', 'отсутствует', 'не указано',
                   '123456', 'ООО \x00 УНИТ', 'ООО \x1f УНИТ', 'ООО \t УНИТ', 'ООО \u200b УНИТ',
                   'ООО \u202e УНИТ', '\ud800', 'ООО 🟥 УНИТ')
        for direction in ('inflow', 'outflow'):
            for name in damaged:
                for bad_position in (0, 1):
                    with self.subTest(direction=direction, name=repr(name), position=bad_position):
                        bodies = [identity_body(direction, '1'), identity_body(direction, '2')]
                        bodies[bad_position][1] = 'Наименование корреспондента: ' + name
                        _, qa = self.assert_blocked(bodies[0]+bodies[1])
                        self.assertEqual(len(qa['identity_admission']['missing_identity_rows']), 1)
                        self.assertIn(name, qa['rows'][bad_position]['detail'][0]['raw_text'])

    def test_legitimate_readable_names_and_continuations(self):
        for name in ('ООО «Синтетический пример»', 'ІП Іваноў І. І.', "O’Connor & Sons", 'A',
                     'UNKNOWN SOLUTIONS', 'Unreadable Books Ltd', 'SYNTHETIC-HOLDER', 'Αλφα ΕΠΕ',
                     'АО «123 & Ко»', 'Cafe\u0301 SA'):
            for direction in ('inflow', 'outflow'):
                with self.subTest(name=name, direction=direction):
                    body=identity_body(direction,name=name)
                    body.insert(2,'                             SYNTHETIC SERVICE UNIT')
                    reviewed,qa=parsed(body)
                    self.assertEqual(sg.identity_admission(sg.build(reviewed))['status'],'admitted')
                    self.assertEqual(qa['rows'][0]['document_view']['counterparty']['name_raw'],
                                     name+'\n                             SYNTHETIC SERVICE UNIT')
                    self.assertEqual(qa['rows'][0]['purpose_lines'],['Exact purpose'])

    def test_damaged_multiline_name_including_explicit_unknown(self):
        for tail in ('�', '---', 'нечитаемо', '□', 'SYNTHETIC \u200b UNIT'):
            for direction in ('inflow','outflow'):
                body=identity_body(direction);body.insert(2,'                             '+tail)
                self.assert_blocked(body)

    def test_explicit_partial_name_annotations_hold_only_affected_row(self):
        annotations = (
            'часть наименования неразборчива', 'часть имени нечитаема',
            'часть названия неизвестна', 'фрагмент наименования отсутствует',
            'окончание имени повреждено', 'наименование частично неразборчиво',
            'нечитаемая часть наименования', 'неизвестный фрагмент имени',
            'наименование не читается', 'имя не указано', 'текст неразборчивый',
            'part of the name is unreadable', 'name partially illegible',
            'portion of name unknown', 'unreadable part of name',
            'name not fully readable', 'name incomplete', 'partial name missing',
            'unknown fragment', 'неразборчиво', 'нечитаемо', 'unknown', 'illegible')
        for direction in ('inflow', 'outflow'):
            for position in (0, 1):
                for annotation in annotations:
                    for multiline in (False, True):
                        with self.subTest(direction=direction, position=position,
                                          annotation=annotation, multiline=multiline):
                            bodies=[identity_body('outflow','1'),identity_body('inflow','2')]
                            bodies[position]=identity_body(direction,str(position+1),
                                name='SYNTHETIC ('+annotation+') COMPANY')
                            if multiline:
                                bodies[position][1]='Наименование корреспондента: SYNTHETIC COMPANY'
                                bodies[position].insert(2,'                             UNIT ('+annotation+')')
                            reviewed,qa=self.assert_blocked(bodies[0]+bodies[1])
                            self.assertEqual(len(qa['identity_admission']['missing_identity_rows']),1)
                            self.assertIn(annotation,qa['rows'][position]['document_view']['counterparty']['name_raw'])
                            self.assertTrue(all(x['document_view']['purpose_raw']=='Exact purpose' and
                                                x['document_view']['payment1c_comment_raw']=='Exact purpose'
                                                for x in qa['rows']))
        # No brackets/whole-line predicate can hide an explicit assertion.
        for name in ('SYNTHETIC часть наименования неразборчива COMPANY',
                     'SYNTHETIC name is unknown COMPANY',
                     'SYNTHETIC (часть наименования\n                             неразборчива) COMPANY'):
            for direction in ('inflow','outflow'):
                self.assert_blocked(identity_body(direction,name=name))

    def test_explicit_partial_name_graph_observations_cannot_admit(self):
        for direction in ('inflow','outflow'):
            reviewed,_=parsed(identity_body(direction));good=sg.build(reviewed)
            for name in ('SYNTHETIC (часть наименования неразборчива) COMPANY',
                         'SYNTHETIC\n UNIT (part of name is unreadable)',
                         'SYNTHETIC [unknown] COMPANY'):
                graph=copy.deepcopy(good);op=next(n for n in graph['nodes'] if n['type']=='Operation')
                cp=op['properties']['counterparty'];cp['name_raw']=name
                party=next(n for n in graph['nodes'] if n['id']==cp['party_id']);party['properties']['name_raw']=name
                for observation in party['observations']:
                    if observation['field']=='name_raw':observation.update(raw=name,normalized=name)
                admission=sg.validate(graph)['processing_admission']
                self.assertEqual(admission['status'],'partial')
                self.assertIn('missing_or_unreadable_full_name',admission['statements'][0]['rows'][0]['gaps'])
                for call in (lambda:sg.combine([graph]),lambda:sg.annotate_rko(graph,{})):
                    with self.assertRaises(ValueError):call()

    def test_readable_brackets_and_words_preserve_complete_name(self):
        names=('UNKNOWN SOLUTIONS','Unreadable Books Ltd','ООО «Нечитаемые книги»',
               'Recognized Books Ltd', 'Readable Books Ltd', 'Fully Recognized Solutions',
               'Partly Readable Books Ltd', 'ООО «Распознанные книги»', 'ООО «Читаемые книги»',
               'SYNTHETIC [SERVICE] COMPANY','SYNTHETIC (NORTH) COMPANY',
               'SYNTHETIC {UNIT} COMPANY','Unknown (Solutions) Ltd',
               'Name Books Ltd','Part of the Story Ltd')
        for direction in ('inflow','outflow'):
            for name in names:
                body=identity_body(direction,name=name)
                body.insert(2,'                             [READABLE UNIT] (BRANCH)')
                reviewed,qa=parsed(body);self.assertTrue(sg.validate(sg.build(reviewed))['valid'])
                self.assertEqual(qa['identity_admission']['status'],'admitted')
                self.assertEqual(qa['rows'][0]['document_view']['counterparty']['name_raw'],
                                 name+'\n                             [READABLE UNIT] (BRANCH)')
                self.assertEqual(qa['rows'][0]['purpose_lines'],['Exact purpose'])

    def test_explicit_recognition_predicate_grammar_and_inflections(self):
        # Cross subject/state families: markers may be embedded and wrapped;
        # legal-name words alone are covered by the independent positives above.
        families = (
            (('наименование', 'полное название', 'часть наименования', 'части имени',
              'фрагменты полного наименования', 'окончание названия', 'буквы имени', 'символы'),
             ('не распознано', 'не распознана', 'не распознаны', 'нераспознанный',
              'не полностью распознано', 'не до конца прочитано', 'непрочитанная',
              'частично распознано', 'распознано частично', 'распознано не полностью',
              'неполностью читаемо', 'читаемо частично', 'разобрано не до конца',
              'не удалось распознать', 'неизвестно', 'повреждено')),
            (('name', 'full name', 'part of the name', 'portion of name',
              'letters of the full name', 'characters', "name's ending"),
             ('not recognized', 'not fully recognized', 'not completely recognised',
              'partially recognized', 'partly recognised', 'incompletely recognized',
              'partly readable', 'partially readable', 'incompletely legible',
              'recognized only partially', 'read only partly', 'not entirely known',
              'unknown', 'unreadable', 'incomplete')))
        for subjects, states in families:
            for subject in subjects:
                for state in states:
                    for separator in (' ', ': ', '\n                             '):
                        value='SYNTHETIC ('+subject+separator+state+') COMPANY'
                        with self.subTest(value=value):
                            self.assertFalse(a.readable_name(value))
        for marker in ('не распознана', 'нераспознанная', 'распознано частично',
                       'not fully recognized', 'partly readable', 'partially recognised',
                       'incompletely recognized', 'readable only partly'):
            self.assertFalse(a.readable_name('SYNTHETIC ['+marker+'] COMPANY'))

    def test_partial_recognition_both_roles_positions_and_source_retention(self):
        annotations=('часть наименования не распознана', 'часть названия не распознана',
                     'часть наименования нераспознана', 'наименование распознано частично',
                     'полное имя распознано не полностью', 'буквы имени не распознаны',
                     'name not fully recognized', 'name partially recognized',
                     'name partly readable', 'name partially readable',
                     'name incompletely recognised', 'letters of name are partly readable',
                     'name is recognized only partially', "name's ending is not fully recognised")
        for direction in ('inflow','outflow'):
            for position in (0,1):
                for annotation in annotations:
                    blocks=[identity_body('outflow','1'),identity_body('inflow','2')]
                    blocks[position]=identity_body(direction,str(position+1),
                        name='SYNTHETIC ('+annotation+') COMPANY')
                    if position:
                        blocks[position][1]='Наименование корреспондента: SYNTHETIC COMPANY'
                        blocks[position].insert(2,'                             UNIT ('+annotation+')')
                    with self.subTest(direction=direction,position=position,annotation=annotation):
                        reviewed,qa=parsed(blocks[0]+blocks[1]);graph=sg.build(reviewed)
                        self.assertTrue(sg.validate(graph)['valid'])
                        ad=sg.identity_admission(graph)['statements'][0]
                        self.assertEqual((ad['status'],ad['ready_count'],ad['clarification_count']),('partial',1,1))
                        self.assertEqual(ad['rows'][position]['status'],'needs_clarification')
                        self.assertTrue(ad['rows'][position]['questions'])
                        ops=[n for n in graph['nodes'] if n['type']=='Operation']
                        self.assertEqual(len(ops),2)
                        self.assertIn(annotation,ops[position]['properties']['counterparty']['name_raw'])
                        raw=next(o['raw'] for o in ops[position]['observations'] if o['field']=='identity_detail_raw')
                        self.assertIn(annotation,raw)
                        self.assertEqual(ops[position]['properties']['purpose_raw'],'Exact purpose')
                        self.assertEqual(ops[position]['properties']['payment1c_comment_raw'],'Exact purpose')

    def test_all_unp_occurrences_inline_and_multiline(self):
        labels=('УНП плательщика:', 'УНП получателя:', 'УНП за кого:')
        for direction in ('inflow','outflow'):
            for label in labels:
                original={'УНП плательщика:':'111222333','УНП получателя:':'444555666','УНП за кого:':''}[label]
                for value in (original,'999888777','???','11122233�',''):
                    for inline in (True,False):
                        with self.subTest(direction=direction,label=label,value=value,inline=inline):
                            body=identity_body(direction)
                            if inline:body[2]+=' '+label+value
                            else:body.insert(3,label+value)
                            _,qa=self.assert_blocked(body)
                            values=qa['rows'][0]['metadata_blocks'][label]
                            self.assertEqual(values,[original,value])
                            self.assertEqual(qa['rows'][0]['purpose_lines'],['Exact purpose'])
                            self.assertEqual(len([x for x in qa['rows'][0]['metadata_assertions'] if x['label']==label]),2)

    def test_name_and_role_labels_not_hidden_in_other_fields(self):
        for direction in ('inflow','outflow'):
            for label in ('Наименование корреспондента:', 'Наименование плательщика:',
                          'Наименование получателя:', 'Наименование за кого:'):
                for value in ('SYNTHETIC COUNTERPARTY','SECOND NAME','�',''):
                    for placement in ('name_inline','unp_inline','separate'):
                        with self.subTest(direction=direction,label=label,value=value,placement=placement):
                            body=identity_body(direction)
                            if placement=='separate':body.insert(3,label+value)
                            else:body[1 if placement=='name_inline' else 2]+=' '+label+value
                            _,qa=self.assert_blocked(body)
                            self.assertEqual(qa['rows'][0]['purpose_lines'],['Exact purpose'])
                            self.assertTrue(any(x['label']==label and x['value_raw']==value
                                                for x in qa['rows'][0]['metadata_assertions']))

    def test_metadata_overlap_damaged_labels_and_valid_split_blocks(self):
        for direction in ('inflow','outflow'):
            body=identity_body(direction);body[2:3]=['УНП плательщика: 111222333','УНП получателя: 444555666','УНП за кого: ']
            reviewed,qa=parsed(body)
            self.assertEqual(sg.identity_admission(sg.build(reviewed))['status'],'admitted')
            self.assertEqual(qa['rows'][0]['purpose_lines'],['Exact purpose'])
            for label in ('УНП получа�теля:','УНП плате?льщика:','Наименование корреспон�дента:','УНП за кого', 'УНП\tполучателя:'):
                damaged=identity_body(direction);damaged.insert(3,label+' 999888777')
                _,qa=self.assert_blocked(damaged)
                self.assertIn('unsupported_or_damaged_identity_label',qa['rows'][0]['metadata_gaps'])
                # Unsupported text stays visible; it never becomes a usable identity.
                self.assertIn(label+' 999888777',qa['rows'][0]['purpose_lines'])
                self.assertEqual(qa['rows'][0]['document_view']['purpose_status'],'unknown')
            def overlap(doc):doc['pages'][1]['lines'][0]['overlaps']=[10]
            duplicate=identity_body(direction);duplicate.insert(3,'УНП получателя: 444555666')
            _,qa=self.assert_blocked(duplicate,overlap)
            self.assertIn('overlapping_metadata_assertion',qa['rows'][0]['metadata_gaps'])
            for unp in ('１２３４５６７８９','١٢٣٤٥٦٧٨٩','11122233\u200b3','11122233�','111222333?'):
                bad=identity_body(direction);bad[2]=bad[2].replace('111222333' if direction=='inflow' else '444555666',unp)
                self.assert_blocked(bad)

    def test_inline_business_prefix_and_full_purpose_preserved(self):
        body=identity_body();body[2]='Business prefix  17.00; '+body[2]
        body[-1]='Exact  purpose 57.00\nсимвол 01: 3.00'
        # Text lines remain text lines, rather than introducing literal escapes.
        body[-1:]=body[-1].split('\n')
        reviewed,qa=parsed(body)
        self.assertEqual(sg.identity_admission(sg.build(reviewed))['status'],'admitted')
        expected='Business prefix  17.00;\nExact  purpose 57.00\nсимвол 01: 3.00'
        self.assertEqual(qa['rows'][0]['document_view']['purpose_raw'],expected)
        self.assertEqual(qa['rows'][0]['document_view']['payment1c_comment_raw'],expected)

    def test_graph_readability_cannot_be_bypassed_by_matching_bad_observations(self):
        reviewed,_=parsed(identity_body());good=sg.build(reviewed)
        for bad_name in ('---','�','нечитаемо','SYNTHETIC УНП получателя:999888777'):
            graph=copy.deepcopy(good);op=next(n for n in graph['nodes'] if n['type']=='Operation')
            cp=op['properties']['counterparty'];cp['name_raw']=bad_name
            party=next(n for n in graph['nodes'] if n['id']==cp['party_id']);party['properties']['name_raw']=bad_name
            for observation in party['observations']:
                if observation['field']=='name_raw':observation.update(raw=bad_name,normalized=bad_name)
            result=sg.validate(graph)
            self.assertEqual(result['processing_admission']['status'],'partial')
            self.assertIn('missing_or_unreadable_full_name',result['processing_admission']['statements'][0]['rows'][0]['gaps'])
            with self.assertRaises(ValueError):sg.require_processing_admission(graph)

    def test_unp_raw_observation_controls_cannot_be_normalized_into_proof(self):
        reviewed,_=parsed(identity_body());good=sg.build(reviewed)
        for raw in ('111\t222333','111222333\r','111\u200b222333','１１１２２２３３３'):
            graph=copy.deepcopy(good);op=next(n for n in graph['nodes'] if n['type']=='Operation')
            party=next(n for n in graph['nodes'] if n['id']==op['properties']['counterparty']['party_id'])
            for observation in party['observations']:
                if observation['field']=='unp_raw':observation['raw']=raw
            result=sg.identity_admission(graph)
            self.assertEqual(result['status'],'partial')
            self.assertIn('counterparty_unp_raw_source_observation_missing_or_ambiguous',result['statements'][0]['rows'][0]['gaps'])
        graph=copy.deepcopy(good);op=next(n for n in graph['nodes'] if n['type']=='Operation')
        party=next(n for n in graph['nodes'] if n['id']==op['properties']['counterparty']['party_id'])
        for observation in party['observations']:
            if observation['field']=='unp_raw':observation['raw']=' 111 222 333 '
        self.assertEqual(sg.identity_admission(graph)['status'],'admitted')

    def test_actual_positive_cli_chain_and_specific_negative_identity_outcomes(self):
        import subprocess,sys
        scripts=Path(__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp)
            def run(script,args,expected):
                proc=subprocess.run([sys.executable,str(scripts/script),*map(str,args)],capture_output=True,text=True,timeout=20)
                self.assertEqual(proc.returncode,expected,proc.stdout+proc.stderr)
                self.assertFalse(proc.stderr,proc.stderr)
                return json.loads(proc.stdout)
            body=identity_body('outflow','1')+identity_body('inflow','2')
            geom=tmp/'geometry.json';geom.write_text(json.dumps(document(body)))
            paths=tmp/'paths.json';paths.write_text(json.dumps([str(geom)]))
            reviewed=tmp/'reviewed.json';graph=tmp/'graph.json';validation=tmp/'validation.json'
            result=run('archive_adapter.py',['assemble',paths,'--dataset-id','synthetic','--output',reviewed,'--qa-output',tmp/'qa.json'],0)
            self.assertTrue(result['output_saved'])
            run('statement_graph.py',['build',reviewed,'--output',graph],0)
            result=run('statement_graph.py',['validate',graph,'--output',validation],0)
            self.assertEqual(result['processing_admission'],'admitted')
            actual=json.loads(graph.read_text());ops=[n['properties'] for n in actual['nodes'] if n['type']=='Operation']
            self.assertEqual([(p['amount'],p['direction'],p['purpose_raw'],p['payment1c_comment_raw']) for p in ops],
                             [('2','outflow','Exact purpose','Exact purpose'),('3','inflow','Exact purpose','Exact purpose')])
            controls=tmp/'controls.json';controls.write_text(json.dumps({'statements':[{'statement_id':'dataset:synthetic:statement:1',
                'opening':'0.00','closing':'1.00','printed_debit_total':'2.00','printed_credit_total':'3.00','no_activity':False,
                'opening_raw':'0.00','first_for_account':True,'first_account_basis':'printed first-statement marker','source_review':{'origin':'independent_original_pdf','review_status':'reviewed','reference':'synthetic review'},'printed_operation_count':{'status':'absent','value':None,'raw':None}}]}))
            result=run('statement_graph.py',['check',graph,'--controls',controls,'--output',tmp/'acceptance.json'],1)
            self.assertEqual(result['status'],'needs_review') # Currency/unit remains unknown.
            check=json.loads((tmp/'acceptance.json').read_text())
            self.assertIn('money_unit_unknown',check['statements'][0]['issues'])
            self.assertTrue(all(sg.decimal(v)==0 for v in check['statements'][0]['differences'].values()))
            # Explicitly resolve only the invented fixture's unit, not real bank data.
            for n in actual['nodes']:
                if n['type']=='CurrencyContext':n['properties'].update(semantics_status='verified',epoch='synthetic-only',unit_raw='BYR',evidence_refs=['synthetic-unit-evidence'])
            known=tmp/'known-unit-graph.json';known.write_text(json.dumps(actual))
            known_controls=json.loads(controls.read_text())
            known_controls['statements'][0]['source_unit_review']={
                'status':'verified','source_unit':'synthetic-unit','source_epoch':'synthetic-epoch',
                'transfer_factor':'1','printed_label_anomaly':False,
                'basis':'Explicit unit for invented synthetic fixture only; no original PDF evidence'}
            controls.write_text(json.dumps(known_controls))
            self.assertEqual(run('statement_graph.py',['check',known,'--controls',controls,'--output',tmp/'known-acceptance.json'],0)['status'],'pass')
            for position in (0,1):
                broken=[identity_body('outflow','1'),identity_body('inflow','2')]
                broken[position][1]='Наименование корреспондента: SYNTHETIC � COMPANY'
                geom.write_text(json.dumps(document(broken[0]+broken[1])))
                partial=tmp/('partial'+str(position)+'.json');q=tmp/('partialqa'+str(position)+'.json')
                result=run('archive_adapter.py',['assemble',paths,'--dataset-id','synthetic','--output',partial,'--qa-output',q],0)
                self.assertEqual(result['status'],'partial');self.assertTrue(result['output_saved'])
                output=tmp/('partialgraph'+str(position)+'.json')
                run('statement_graph.py',['build',partial,'--output',output],0)
                result=run('statement_graph.py',['validate',output,'--output',tmp/('partialvalidation'+str(position)+'.json')],0)
                self.assertEqual(result['processing_admission'],'partial')
                g=json.loads(output.read_text());ad=sg.identity_admission(g)['statements'][0]
                self.assertEqual((ad['ready_count'],ad['clarification_count'],ad['operation_count']),(1,1,2))
                result=run('statement_graph.py',['check',output,'--controls',controls,'--output',tmp/('partialcheck'+str(position)+'.json')],1)
                self.assertEqual(result['status'],'needs_review')
                # Absent field TYPES block the whole source and never save accepted output.
            for field in ('name','unp'):
                absent=[]
                for direction,number in (('inflow','1'),('outflow','2')):
                    body=identity_body(direction,number)
                    body.pop(1 if field=='name' else 2);absent.extend(body)
                geom.write_text(json.dumps(document(absent)))
                output=tmp/('absent-'+field+'.json')
                result=run('archive_adapter.py',['assemble',paths,'--dataset-id','synthetic','--output',output,'--qa-output',tmp/('absentqa-'+field+'.json')],2)
                self.assertEqual(result['status'],'blocked_format');self.assertFalse(output.exists())
                raw,_=parsed(absent);source=tmp/('absentsource-'+field+'.json');source.write_text(json.dumps(raw))
                self.assertEqual(run('statement_graph.py',['build',source,'--output',output],2)['status'],'error')
                self.assertFalse(output.exists())


class PartialFormatAndClarificationTests(unittest.TestCase):
    def test_all_values_blank_proves_types_and_holds_every_row(self):
        body=identity_body('inflow','1',name='',payer='',payee='')+identity_body('outflow','2',name='',payer='',payee='')
        reviewed,qa=parsed(body);graph=sg.build(reviewed);ad=sg.validate(graph)
        self.assertTrue(ad['valid']);state=ad['processing_admission']['statements'][0]
        self.assertEqual(state['field_types']['status'],'supported')
        self.assertEqual((state['ready_count'],state['clarification_count'],state['operation_count']),(0,2,2))
        self.assertEqual([r['status'] for r in state['rows']],['needs_clarification']*2)
        self.assertTrue(all(r['questions'] for r in state['rows']))
        self.assertEqual(len(qa['rows']),2)
        for row in [n for n in graph['nodes'] if n['type']=='Operation']:
            self.assertTrue(any(o['field']=='identity_detail_raw' and 'УНП плательщика:' in o['raw'] for o in row['observations']))
        controls={'statements':[{'statement_id':'dataset:synthetic:statement:1','opening':'0.00','closing':'1.00',
                    'printed_debit_total':'2.00','printed_credit_total':'3.00','no_activity':False,
                    'opening_raw':'0.00','first_for_account':True,'first_account_basis':'synthetic'}]}
        check=sg.check_conversion(graph,controls)
        self.assertEqual(check['status'],'needs_review');self.assertIn('row_clarification_required',check['statements'][0]['issues'])
        self.assertTrue(all(sg.decimal(v)==0 for v in check['statements'][0]['differences'].values()))

    def test_one_row_missing_label_does_not_remove_type_proven_elsewhere(self):
        for field in (1,2):
            for position in (0,1):
                bodies=[identity_body('inflow','1'),identity_body('outflow','2')]
                bodies[position].pop(field)
                reviewed,qa=parsed(bodies[0]+bodies[1]);graph=sg.build(reviewed)
                ad=sg.identity_admission(graph)['statements'][0]
                self.assertEqual(ad['field_types']['status'],'supported')
                self.assertEqual((ad['ready_count'],ad['clarification_count']),(1,1))
                self.assertEqual(len(qa['rows']),2)

    def test_coverage_and_source_capability_cannot_be_summary_only(self):
        reviewed,_=parsed(identity_body('inflow','1')+identity_body('outflow','2'));good=sg.build(reviewed)
        for defect in ('dropped_row','missing_raw','summary_only','wrong_label','stale_ready','missing_processing'):
            g=copy.deepcopy(good);op=next(n for n in g['nodes'] if n['type']=='Operation')
            if defect=='dropped_row':
                g['nodes'].remove(op);g['edges']=[e for e in g['edges'] if e['from']!=op['id'] and e['to']!=op['id']]
            if defect=='missing_raw':
                for n in g['nodes']:
                    if n['type']=='Operation':n['observations']=[o for o in n['observations'] if o['field']!='identity_detail_raw']
            if defect=='summary_only':
                for n in g['nodes']:
                    if n['type']=='Statement':n['observations']=[o for o in n['observations'] if o['field']!='counterparty_field_type']
            if defect=='wrong_label':
                for n in g['nodes']:
                    for o in n['observations']:
                        if o['field']=='counterparty_field_type':o['raw']='Наименование SYNTHETIC HOLDER'
            if defect=='stale_ready':op['properties']['processing']['status']='needs_clarification'
            if defect=='missing_processing':del op['properties']['processing']
            with self.subTest(defect=defect):
                with self.assertRaises(ValueError):sg.require_processing_admission(g)

    def test_owner_answer_resolves_held_row_without_mutating_source_and_cli(self):
        import sys,subprocess
        for direction in ('inflow','outflow'):
            reviewed,_=parsed(identity_body(direction,name='SYNTHETIC [unknown]',payer='???',payee='???')+identity_body('inflow','2'))
            original=sg.build(reviewed);held=next(n for n in original['nodes'] if n['type']=='Operation')
            raw=next(o['raw'] for o in held['observations'] if o['field']=='identity_detail_raw')
            answer={'role':'payer' if direction=='inflow' else 'payee','name_raw':'SYNTHETIC CONFIRMED COMPANY',
                    'unp_raw':'999888777','original_detail_raw':raw,'answered_by':'synthetic-owner',
                    'answered_at':'2026-10-08T09:00:00Z','answer_ref':'synthetic-answer-1','confirmation_ref':'synthetic-confirmation-1'}
            saved=copy.deepcopy(original);resolved=sg.clarify_counterparties(original,{held['id']:answer})
            self.assertEqual(original,saved);self.assertTrue(sg.validate(resolved)['valid'])
            final=next(n for n in resolved['nodes'] if n['id']==held['id'])
            self.assertEqual(final['observations'],held['observations'])
            self.assertEqual(final['properties']['counterparty'],held['properties']['counterparty'])
            self.assertEqual(final['properties']['counterparty']['status'],'unknown')
            self.assertEqual(final['properties']['processing']['status'],'ready')
            self.assertEqual(final['properties']['processing']['effective_counterparty']['origin'],'owner_confirmed')
            self.assertEqual(final['properties']['processing']['effective_counterparty']['unp_raw'],'999888777')
            for defect in ('no_confirmation','wrong_raw','wrong_role','unreadable_answer','missing_time'):
                bad=copy.deepcopy(answer)
                if defect=='no_confirmation':del bad['confirmation_ref']
                if defect=='wrong_raw':bad['original_detail_raw']='Different original'
                if defect=='wrong_role':bad['role']='payee' if direction=='inflow' else 'payer'
                if defect=='unreadable_answer':bad['name_raw']='[unknown]'
                if defect=='missing_time':bad['answered_at']='2026-10-08'
                with self.subTest(direction=direction,defect=defect):
                    with self.assertRaises(ValueError):sg.clarify_counterparties(original,{held['id']:bad})
            with tempfile.TemporaryDirectory() as tmp:
                tmp=Path(tmp);source=tmp/'source.json';source.write_text(json.dumps(original));answers=tmp/'answers.json';answers.write_text(json.dumps({held['id']:answer}));out=tmp/'resolved.json'
                proc=subprocess.run([sys.executable,str(Path(__file__).parent/'statement_graph.py'),'clarify-counterparties',str(source),'--annotations',str(answers),'--output',str(out)],capture_output=True,text=True,timeout=20)
                self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
                self.assertEqual(json.loads(out.read_text()),resolved)
                self.assertEqual(json.loads(source.read_text()),original)
            # Unsupported/legacy format cannot use answers as a hidden admission bypass.
            legacy=copy.deepcopy(original)
            for n in legacy['nodes']:
                if n['type']=='Statement':n['observations']=[]
            with self.assertRaises(ValueError):sg.clarify_counterparties(legacy,{held['id']:answer})

    def test_owner_answer_cannot_silently_change_already_recognized_value(self):
        reviewed,_=parsed(identity_body(name='???'));original=sg.build(reviewed);held=next(n for n in original['nodes'] if n['type']=='Operation')
        raw=next(o['raw'] for o in held['observations'] if o['field']=='identity_detail_raw')
        answer={'role':'payer','name_raw':'SYNTHETIC CONFIRMED','unp_raw':'999888777','original_detail_raw':raw,
                'answered_by':'synthetic-owner','answered_at':'2026-10-08T09:00:00Z','answer_ref':'synthetic-answer','confirmation_ref':'synthetic-confirmation'}
        with self.assertRaises(ValueError):sg.clarify_counterparties(original,{held['id']:answer})

class AdditionalClaimTests(unittest.TestCase):
    def test_B_currency_compatibility_gates_fee_only_and_equation(self):
        for gross in ('', 'gross 1000 BYR; '):
            for unit, included in (('USD',False),('EUR',False),('BYN',False),('Р УБ.',False),('BYR',True),('',True)):
                purpose=('Перевод денежных средств; '+gross+'комиссия 15 '+unit).rstrip()
                body=identity_body();body[0]=row('1',credit='985');body[-1]=purpose
                reviewed,qa=parsed(body);graph=sg.build(reviewed)
                op=reviewed['statements'][0]['operations'][0]['properties']
                self.assertEqual((op['amount'],op['direction'],op['payment1c_comment_raw']),('985','inflow',purpose))
                self.assertEqual(op['settlement']['status'],('verified_equation' if gross else 'verified_withheld_fee') if included else 'unknown')
                fee=next(n['properties'] for n in reviewed['entities'] if n['type']=='AmountComponent' and n['properties']['kind']=='fee')
                self.assertEqual(fee['inclusion'],'included' if included else 'unknown')
                if unit in ('USD','EUR','BYN','BYR'):self.assertEqual(fee['currency_raw'],unit)
                self.assertTrue(sg.validate(graph)['valid'])
                self.assertEqual(sg.identity_admission(graph)['status'],'admitted')
                if not included:
                    self.assertIn('component_unit_incompatible_or_unproven',qa['rows'][0]['component_gaps'])
                    forged=copy.deepcopy(graph);fo=next(n for n in forged['nodes'] if n['type']=='Operation')
                    fo['properties']['settlement'].update(status='verified_withheld_fee',net='985',commission='15')
                    self.assertFalse(sg.validate(forged)['valid'])

    def test_N_unknown_opposite_cell_rejects_asserted_movement_both_entrypoints(self):
        for direction, opposite in (('inflow','printed_debit'),('outflow','printed_credit')):
            for raw,status,value,valid in (('', 'absent',None,True),('0.00','observed','0.00',True),('??.??','unknown',None,False)):
                reviewed,_=parsed(identity_body(direction)+identity_body('outflow' if direction=='inflow' else 'inflow','2'))
                op=reviewed['statements'][0]['operations'][0];op['properties'][opposite]=value
                next(o for o in op['observations'] if o['field']==opposite).update(raw=raw,normalized=value,status=status)
                if valid:
                    graph=sg.build(reviewed)
                else:
                    with self.assertRaises(ValueError):sg.build(reviewed)
                    graph=diagnostic(reviewed)
                self.assertEqual(sg.validate(graph)['valid'],valid)
                self.assertEqual(sg.identity_admission(graph)['statements'][0]['ready_count'],2 if valid else 1)
                baseline,_=parsed(identity_body(direction));persisted=sg.build(baseline)
                node=next(n for n in persisted['nodes'] if n['type']=='Operation');node['properties'][opposite]=value
                next(o for o in node['observations'] if o['field']==opposite).update(raw=raw,normalized=value,status=status)
                self.assertEqual(sg.validate(persisted)['valid'],valid)
            damaged=identity_body(direction);damaged[0]=row('1',credit='3' if direction=='inflow' else '??.??',debit='2' if direction=='outflow' else '??.??')
            reviewed,_=parsed(damaged+identity_body('inflow','2'));graph=sg.build(reviewed)
            op=reviewed['statements'][0]['operations'][0]['properties']
            self.assertEqual((op['amount'],op['direction']),(None,'unknown'))
            self.assertTrue(sg.validate(graph)['valid']);self.assertEqual(sg.identity_admission(graph)['statements'][0]['ready_count'],1)

    def test_l_rate_only_basis_never_borrows_later_fee(self):
        for tail,expected in (('2 %; комиссия 15',None),('2 процента; комиссия 15',None),('1000 BYR; комиссия 15','1000')):
            purpose='Комис.вознагр.за прием налич. денег с суммы оборотов '+tail
            body=identity_body('outflow');body[0]=row('1',debit='20');body[-1]=purpose
            reviewed,_=parsed(body);graph=sg.build(reviewed)
            components=[n['properties'] for n in reviewed['entities'] if n['type']=='AmountComponent']
            self.assertEqual([(x['kind'],x['value'],x['inclusion']) for x in components],[('basis',expected,'unknown')])
            op=reviewed['statements'][0]['operations'][0]['properties']
            self.assertEqual((op['amount'],op['direction'],op['purpose_raw']),('20','outflow',purpose))
            self.assertEqual(op['settlement']['status'],'not_applicable');self.assertTrue(sg.validate(graph)['valid'])

    def test_r_each_turnover_overlap_retains_unknown_control(self):
        for field,start,end in (('printed_debit_total',40,62),('printed_credit_total',62,84)):
            for overlap in (False,True):
                def edit(doc):
                    footer=next(x for p in doc['pages'] for x in p['lines'] if x['text'].startswith('ОБОРОТЫ'))
                    footer['glyphs']=[{'cell':i,'text':c} for i,c in enumerate(footer['text'])]
                    if overlap:
                        cell=end-4;footer['glyphs'].append({'cell':cell,'text':'1'});footer['glyphs'].sort(key=lambda g:g['cell']);footer['overlaps']=[cell]
                reviewed,_=parsed(identity_body('outflow')+identity_body('inflow','2'),edit)
                st=reviewed['statements'][0];self.assertEqual(st['properties'][field],None if overlap else ('2.00' if start==40 else '3.00'))
                observation=next(o for o in st['observations'] if o['field']==field)
                self.assertEqual(observation['status'],'unknown' if overlap else 'observed')
                other='printed_credit_total' if start==40 else 'printed_debit_total'
                self.assertIsNotNone(st['properties'][other]);self.assertTrue(sg.validate(sg.build(reviewed))['valid'])
                self.assertEqual([o['properties']['amount'] for o in st['operations']],['2','3'])

    def test_QptV_bounded_damaged_keywords_are_metadata_and_hold_only_row(self):
        for direction in ('inflow','outflow'):
            role='плательщика' if direction=='inflow' else 'получателя'
            for damaged in ('У?П '+role+': 999888777','Наименован?е корреспондента: CONFLICTING NAME'):
                body=identity_body(direction);body.insert(3,damaged)
                reviewed,qa=parsed(body+identity_body('inflow','2'));graph=sg.build(reviewed)
                ad=sg.identity_admission(graph)['statements'][0]
                self.assertEqual((ad['status'],ad['ready_count'],ad['clarification_count']),('partial',1,1))
                op=reviewed['statements'][0]['operations'][0]
                self.assertNotIn(damaged,op['properties']['purpose_raw'])
                self.assertIn(damaged,next(o['raw'] for o in op['observations'] if o['field']=='identity_detail_raw'))
                self.assertIn(damaged,[x['raw_span'] for x in qa['rows'][0]['metadata_assertions']])
                self.assertTrue(sg.validate(graph)['valid'])

    def test_QptY_established_signature_trailers_preserve_rows_and_purpose(self):
        for prefix in a.SIGNATURE_PREFIXES:
            def edit(doc):doc['pages'][-1]['lines'].append(line(prefix+' SYNTHETIC'))
            reviewed,qa=parsed(identity_body()+identity_body('outflow','2'),edit)
            graph=sg.build(reviewed);self.assertTrue(sg.validate(graph)['valid'])
            self.assertEqual(sg.identity_admission(graph)['statements'][0]['ready_count'],2)
            self.assertEqual([o['properties']['purpose_raw'] for o in reviewed['statements'][0]['operations']],['Exact purpose']*2)
            self.assertEqual(qa['residual_business_lines'],[])
        def arbitrary(doc):doc['pages'][-1]['lines'].append(line('UNRECOGNIZED BUSINESS TRAILER'))
        with self.assertRaises(ValueError):parsed(identity_body(),arbitrary)

    def test_Qptb_brutto_conflicts_hold_settlement_not_bank_movement(self):
        for label in ('брутто','brutto','сумма брутто','gross'):
            for total,expected in (('900','unknown'),('1000','verified_equation')):
                purpose='Эквайринг '+label+' '+total+'; комиссия 15'
                body=identity_body();body[0]=row('1',credit='985');body[-1]=purpose
                reviewed,_=parsed(body);op=reviewed['statements'][0]['operations'][0]['properties']
                self.assertEqual(op['settlement']['status'],expected)
                self.assertEqual((op['amount'],op['direction'],op['payment1c_comment_raw']),('985','inflow',purpose))
                self.assertTrue(sg.validate(sg.build(reviewed))['valid'])
                gross=next(n['properties'] for n in reviewed['entities'] if n['type']=='AmountComponent' and n['properties']['kind']=='gross')
                self.assertEqual(gross['value'],total)

    def test_R3xg_party_observation_uses_own_raw_readability(self):
        for direction in ('inflow','outflow'):
            for value,status in (('','absent'),('???','unknown'),('123456789','observed')):
                kwargs={'payee':value} if direction=='inflow' else {'payer':value}
                reviewed,_=parsed(identity_body(direction,**kwargs));role='payee' if direction=='inflow' else 'payer'
                party=next(n for n in reviewed['entities'] if n['alias'].endswith(':'+role))
                self.assertEqual(next(o for o in party['observations'] if o['field']=='unp_raw')['status'],status)
                if value!='???':
                    graph=sg.build(reviewed);self.assertTrue(sg.validate(graph)['valid']);self.assertEqual(sg.identity_admission(graph)['status'],'admitted')

    def test_R3xl_unsolicited_owner_answer_cannot_change_source_provenance(self):
        for direction in ('inflow','outflow'):
            reviewed,_=parsed(identity_body(direction));graph=sg.build(reviewed)
            op=next(n for n in graph['nodes'] if n['type']=='Operation');cp=op['properties']['counterparty']
            raw=next(o['raw'] for o in op['observations'] if o['field']=='identity_detail_raw')
            answer=dict(role=cp['role'],name_raw=cp['name_raw'],unp_raw=cp['unp_raw'],original_detail_raw=raw,
                        answered_by='synthetic-owner',answered_at='2026-10-08T00:00:00Z',answer_ref='private:answer',confirmation_ref='private:confirmation')
            with self.assertRaises(ValueError):sg.clarify_counterparties(graph,{op['id']:answer})
            op['properties']['counterparty_clarification']=answer;sg.materialize_processing(graph)
            self.assertNotEqual(op['properties']['processing']['effective_counterparty']['origin'],'owner_confirmed')
            self.assertFalse(sg.validate(graph)['valid'])

    def test_R3xv_damaged_structural_closing_is_unknown_without_row_loss(self):
        def edit(doc):doc['pages'][-1]['lines'][-1]=line('Исходящее сальдо ??.??')
        reviewed,qa=parsed(identity_body()+identity_body('outflow','2'),edit)
        st=reviewed['statements'][0];self.assertIsNone(st['properties']['closing'])
        self.assertEqual(next(o for o in st['observations'] if o['field']=='closing'),a.obs('closing','??.??',None,'unknown'))
        self.assertEqual(len(st['operations']),2);self.assertEqual(qa['operations'],2)
        graph=sg.build(reviewed);self.assertTrue(sg.validate(graph)['valid']);self.assertEqual(sg.check_conversion(graph,{'statements':[]})['status'],'needs_review')

    def test_R3x4_intact_type_label_survives_only_value_overlap(self):
        body=[row('1',debit='2'),'Наименование корреспондента: FIRST SYNTHETIC','УНП получателя: 444555666','First purpose',
              row('2',credit='3'),'Наименование корреспондента: SECOND SYNTHETIC','УНП плательщика: 111222333','Second purpose']
        for cell,expected in ((21,'partial'),(2,'blocked')):
            def edit(doc):
                for page in doc['pages']:
                    for item in page['lines']:
                        if item['raw_text'].startswith('УНП получателя:'):item['overlaps']=[cell]
            reviewed,qa=parsed(body,edit);graph=diagnostic(reviewed);ad=sg.identity_admission(graph)['statements'][0]
            self.assertEqual(ad['status'],expected);self.assertEqual(len(reviewed['statements'][0]['operations']),2)
            if expected=='partial':
                self.assertEqual((ad['ready_count'],ad['clarification_count']),(1,1));self.assertTrue(sg.validate(sg.build(reviewed))['valid'])
            else:
                self.assertIn('payee_unp',ad['field_types']['missing'])
                with self.assertRaises(ValueError):sg.build(reviewed)


if __name__ == "__main__":unittest.main()
