"""Русификатор WORKS: Motorsport Factory (неофициальный).

Не содержит файлов игры: читает ваши собственные resources/app.asar и WORKS.exe,
добавляет русский язык ('ru' / «Русский») в словарь игры и интерфейс, пересчитывает
хеши целостности ASAR и обновляет встроенный SHA-256 заголовка в WORKS.exe.
Оригиналы сохраняются рядом с суффиксом .orig.

Режимы:
  WORKS_RU_Installer.exe                         - окно установщика
  WORKS_RU_Installer.exe "<путь>\\WORKS.exe" ... - тихий режим для параметров запуска Steam:
                                                   "<путь>\\WORKS_RU_Installer.exe" %command%
"""
import copy
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from collections import Counter

if getattr(sys, 'frozen', False):
    BASE = os.path.join(sys._MEIPASS, 'data')
else:
    BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

EXE_NAME = 'WORKS.exe'
ASAR_REL = os.path.join('resources', 'app.asar')
BLOCK_SIZE = 4194304
MARKER = 'works:ru-init'
PLACEHOLDER_RE = re.compile(r'\{[A-Za-z0-9_]+\}')


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------- discovery

def is_game_dir(p):
    return (bool(p)
            and os.path.isfile(os.path.join(p, EXE_NAME))
            and os.path.isfile(os.path.join(p, ASAR_REL)))


def steam_libraries():
    libs = []
    try:
        import winreg
        for hive, key, val in (
            (winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath'),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    libs.append(winreg.QueryValueEx(k, val)[0])
            except OSError:
                pass
    except ImportError:
        return []
    out = []
    for root in libs:
        out.append(root)
        vdf = os.path.join(root, 'steamapps', 'libraryfolders.vdf')
        try:
            txt = open(vdf, encoding='utf-8', errors='replace').read()
            out += [m.replace('\\\\', '\\') for m in re.findall(r'"path"\s+"([^"]+)"', txt)]
        except OSError:
            pass
    seen, res = set(), []
    for p in out:
        p = os.path.normpath(p)
        if p.lower() not in seen:
            seen.add(p.lower())
            res.append(p)
    return res


def find_game():
    cands = []
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
    for p in (here, os.path.dirname(here), os.path.join(here, 'game'), os.path.join(os.path.dirname(here), 'game')):
        cands.append(p)
    for lib in steam_libraries():
        common = os.path.join(lib, 'steamapps', 'common')
        try:
            for d in os.listdir(common):
                dl = d.lower()
                if dl == 'works' or dl.startswith('works ') or dl.startswith('works:'):
                    cands.append(os.path.join(common, d))
        except OSError:
            pass
    for c in cands:
        if is_game_dir(c):
            return c
    return ''


# ---------------------------------------------------------------- ASAR helpers

def read_asar_index(asar_path):
    with open(asar_path, 'rb') as f:
        hdr_prefix = struct.unpack('<4I', f.read(16))
        if hdr_prefix[0] != 4:
            raise Unsupported('неизвестный заголовок app.asar')
        hlen = hdr_prefix[3]
        hdr_bytes = f.read(hlen)
        hdr_hash = hashlib.sha256(hdr_bytes).hexdigest()
        header = json.loads(hdr_bytes.decode('utf-8'))
        data_start = 16 + ((hlen + 3) & ~3)
    files = {}

    def walk(node, path=''):
        if 'files' in node:
            for k, v in node['files'].items():
                walk(v, f'{path}/{k}' if path else k)
        else:
            files[path] = node

    walk(header)
    return header, hdr_hash, data_start, files


def read_asar_member(asar_path, data_start, files, path):
    node = files.get(path)
    if not node or node.get('unpacked') or 'offset' not in node:
        raise Unsupported('в app.asar не найден файл ' + path)
    with open(asar_path, 'rb') as f:
        f.seek(data_start + int(node['offset']))
        return f.read(node['size'])


def compute_integrity(data):
    full_hash = hashlib.sha256(data).hexdigest()
    blocks = [hashlib.sha256(data[i:i + BLOCK_SIZE]).hexdigest() for i in range(0, len(data), BLOCK_SIZE)]
    if not blocks:
        blocks = [full_hash]
    return {'algorithm': 'SHA256', 'hash': full_hash, 'blockSize': BLOCK_SIZE, 'blocks': blocks}


def state(game):
    asar_path = os.path.join(game, ASAR_REL)
    if not is_game_dir(game):
        raise Unsupported('не найдены WORKS.exe и resources/app.asar')
    _, _, data_start, files = read_asar_index(asar_path)
    if 'dist/index.html' not in files:
        raise Unsupported('в app.asar отсутствует dist/index.html')
    html = read_asar_member(asar_path, data_start, files, 'dist/index.html').decode('utf-8', 'replace')
    return 'patched' if MARKER in html else 'clean'


# ---------------------------------------------------------------- JS dict / merge

def parse_js_dict(text):
    pos = 1
    n = len(text)
    out = {}
    escapes = {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', '`': '`', '"': '"', "'": "'"}
    while pos < n:
        ch = text[pos]
        if ch == '}':
            break
        if ch in ', \n\r\t':
            pos += 1
            continue
        if ch == '"':
            end_k = text.find('"', pos + 1)
            k = text[pos + 1:end_k]
            pos = end_k + 1
        else:
            end_k = text.find(':', pos)
            k = text[pos:end_k].strip()
            pos = end_k
        while text[pos] in ' :':
            pos += 1
        q = text[pos]
        pos += 1
        buf = []
        while pos < n:
            c = text[pos]
            if c == '\\':
                nxt = text[pos + 1]
                buf.append(escapes.get(nxt, nxt))
                pos += 2
            elif c == q:
                pos += 1
                break
            else:
                buf.append(c)
                pos += 1
        out[k] = ''.join(buf)
    return out


def extract_dict_literal(src, start_idx):
    """Return (end_idx_after_brace, raw_object_text) starting at '{'."""
    i = start_idx
    n = len(src)
    assert src[i] == '{'
    i += 1
    while i < n:
        c = src[i]
        if c == '}':
            return i + 1, src[start_idx:i + 1]
        if c in ('"', "'", '`'):
            q = c
            i += 1
            while i < n:
                if src[i] == '\\':
                    i += 2
                elif src[i] == q:
                    i += 1
                    break
                else:
                    i += 1
        else:
            i += 1
    raise Unsupported('не удалось разобрать словарь локализации')


def merge_ru_dict(en_dict, pl_dict, ru_src):
    omitted_by_pl = {}
    for k, ev in en_dict.items():
        if k in pl_dict:
            ep = set(PLACEHOLDER_RE.findall(ev))
            pp = set(PLACEHOLDER_RE.findall(pl_dict[k]))
            if ep - pp:
                omitted_by_pl[k] = ep - pp

    def valid_entry(ev, rv):
        if not isinstance(rv, str) or not rv.strip():
            return False
        ep = Counter(PLACEHOLDER_RE.findall(ev))
        rp = Counter(PLACEHOLDER_RE.findall(rv))
        if any(ph not in ep for ph in rp):
            return False
        return True

    merged = {}
    done, fallback = 0, 0
    for k, ev in en_dict.items():
        rv = ru_src.get(k)
        if rv is not None and valid_entry(ev, rv):
            ep = Counter(PLACEHOLDER_RE.findall(ev))
            rp = Counter(PLACEHOLDER_RE.findall(rv))
            allowed = omitted_by_pl.get(k, set())
            ok = all(
                (rp.get(ph, 0) <= cnt if ph in allowed else rp.get(ph, 0) == cnt)
                for ph, cnt in ep.items()
            )
            if ok:
                merged[k] = rv
                done += 1
                if k.endswith('#one'):
                    base = k[:-4]
                    for suf in ('#few', '#many'):
                        pk = base + suf
                        if pk in ru_src and valid_entry(en_dict.get(base + '#other', ev), ru_src[pk]):
                            merged[pk] = ru_src[pk]
                continue
        merged[k] = ev
        fallback += 1
    return merged, done, fallback


# ---------------------------------------------------------------- patchers

def replace_optional(src, pairs):
    applied = 0
    for old, new in pairs:
        if old in src:
            src = src.replace(old, new)
            applied += 1
    return src, applied


def patch_bridge(bridge, ru_src, log):
    # Locate language list, display names, language map, and active getter
    m = re.search(
        r'([A-Za-z0-9_$]+)=\[`en`,([^\]]+)\],'
        r'([A-Za-z0-9_$]+)=\{en:`English`,([^}]+)\},'
        r'([A-Za-z0-9_$]+)=\{en:([A-Za-z0-9_$]+),([^}]+pl:([A-Za-z0-9_$]+))\}',
        bridge
    )
    if not m:
        raise Unsupported('не найдена таблица языков в releaseGameBridge')
    f_var, f_rest, i_var, i_rest, l_var, e_var, l_rest, p_var = m.groups()

    # Extract EN and PL dicts for update-safe merge-by-key
    m_e = re.search(r'(?:var |,)' + re.escape(e_var) + r'=\{', bridge[:m.start()])
    m_p = re.search(r'(?:var |,)' + re.escape(p_var) + r'=\{', bridge[:m.start()])
    if not m_e or not m_p:
        raise Unsupported('не найдены словари EN/PL в releaseGameBridge')
    _, e_raw = extract_dict_literal(bridge, m_e.end() - 1)
    _, p_raw = extract_dict_literal(bridge, m_p.end() - 1)
    en_dict = parse_js_dict(e_raw)
    pl_dict = parse_js_dict(p_raw)
    if len(en_dict) < 1000:
        raise Unsupported('слишком мало ключей в словаре игры (%d)' % len(en_dict))

    merged_ru, done, fallback = merge_ru_dict(en_dict, pl_dict, ru_src)
    log('Словарь: переведено %d ключей, оставлено на английском %d.' % (done, fallback))
    ru_literal = json.dumps(merged_ru, ensure_ascii=False, separators=(',', ':'))

    new_langs = (
        f'RuDict={ru_literal},'
        f'{f_var}=[`en`,`ru`,{f_rest}],'
        f'{i_var}={{en:`English`,ru:`Русский`,{i_rest}}},'
        f'{l_var}={{en:{e_var},ru:RuDict,{l_rest}}}'
    )
    bridge = bridge[:m.start()] + new_langs + bridge[m.end():]

    # Active locale getter B()
    m_get = re.search(r'function ([A-Za-z0-9_$]+)\(\)\{return [A-Za-z0-9_$]+\}function [A-Za-z0-9_$]+\([A-Za-z0-9_$]+\)\{return typeof', bridge)
    if not m_get:
        raise Unsupported('не найден геттер языка в releaseGameBridge')
    b_fn = m_get.group(1)

    # Plural rules
    m_pl = re.search(
        r'new Intl\.PluralRules\(`pt-BR`\),([A-Za-z0-9_$]+)=new Intl\.PluralRules\(`pl`\);'
        r'function ([A-Za-z0-9_$]+)\(([A-Za-z0-9_$]+),([A-Za-z0-9_$]+)\)\{return \3===`pl`\?\1\.select\(\4\):',
        bridge
    )
    if not m_pl:
        raise Unsupported('не найдены правила множественного числа в releaseGameBridge')
    pl_v, fn_v, a1, a2 = m_pl.groups()
    old_pl = m_pl.group(0)
    new_pl = (
        f'new Intl.PluralRules(`pt-BR`),{pl_v}=new Intl.PluralRules(`pl`),ruPl=new Intl.PluralRules(`ru`);'
        f'function {fn_v}({a1},{a2}){{return {a1}===`ru`?ruPl.select({a2}):{a1}===`pl`?{pl_v}.select({a2}):'
    )
    bridge = bridge.replace(old_pl, new_pl, 1)

    # First-launch default ru
    m_lc = re.search(
        r'function ([A-Za-z0-9_$]+)\(\)\{try\{let ([A-Za-z0-9_$]+)=localStorage\.getItem\(([A-Za-z0-9_$]+)\);if\(([A-Za-z0-9_$]+)\(\2\)\)return \2\}catch\{\}',
        bridge
    )
    if not m_lc:
        raise Unsupported('не найдена инициализация языка в releaseGameBridge')
    fn_lc, tmp_e, key_ac, chk_v = m_lc.groups()
    old_lc = m_lc.group(0)
    new_lc = (
        f'function {fn_lc}(){{try{{if(!localStorage.getItem(`{MARKER}`)){{'
        f'localStorage.setItem(`{MARKER}`,`1`);localStorage.setItem({key_ac},`ru`);return`ru`}}'
        f'let {tmp_e}=localStorage.getItem({key_ac});if({chk_v}({tmp_e}))return {tmp_e}}}catch{{}}'
    )
    bridge = bridge.replace(old_lc, new_lc, 1)

    # Best-effort extra Phaser & duration formatting
    bridge, _ = replace_optional(bridge, [
        (
            'let t=Math.floor(e/G),n=e%G/G,r=Math.floor(n*24),i=Math.floor((n*24-r)*60);return t>0?`${t}d ${r}h`:r>0?`${r}h ${i}m`:`${i}m`',
            f'let t=Math.floor(e/G),n=e%G/G,r=Math.floor(n*24),i=Math.floor((n*24-r)*60);return {b_fn}()===`ru`?(t>0?`${{t}}д ${{r}}ч`:r>0?`${{r}}ч ${{i}}м`:`${{i}}м`):(t>0?`${{t}}d ${{r}}h`:r>0?`${{r}}h ${{i}}m`:`${{i}}m`)'
        ),
        ('u.add.text(o.x,o.y,`KEEP CLEAR`,', f'u.add.text(o.x,o.y,{b_fn}()===`ru`?`НЕ ЗАНИМАТЬ`:`KEEP CLEAR`,'),
        ('u.add.text(x.x,x.y,`BAY ${a}\\nKEEP CLEAR`,', f'u.add.text(x.x,x.y,{b_fn}()===`ru`?`БОКС ${{a}}\\nНЕ ЗАНИМАТЬ`:`BAY ${{a}}\\nKEEP CLEAR`,'),
        ('this.bayUpgradeLabel||=this.add.text(0,0,`UPGRADES`,{fontFamily:`Barlow Condensed, sans-serif`,',
         f'this.bayUpgradeLabel||=this.add.text(0,0,{b_fn}()===`ru`?`УЛУЧШ.`:`UPGRADES`,{{fontFamily:`Barlow Condensed, "Open Sans", sans-serif`,'),
    ])
    return bridge, ru_literal


def patch_sim_host(sim, ru_literal):
    m = re.search(
        r'var ([A-Za-z0-9_$]+)=\[`en`,([^\]]+)\];var ([A-Za-z0-9_$]+)=`en`;'
        r'var ([A-Za-z0-9_$]+)=\{en:([A-Za-z0-9_$]+),([^}]+)\};',
        sim
    )
    if not m:
        raise Unsupported('не найдена таблица языков в simulation-host.cjs')
    at_v, at_rest, jt_v, mt_v, xt_v, mt_rest = m.groups()
    new_sim_langs = (
        f'var RuDict={ru_literal};'
        f'var {at_v}=[`en`,`ru`,{at_rest}];var {jt_v}=`en`;'
        f'var {mt_v}={{en:{xt_v},ru:RuDict,{mt_rest}}};'
    )
    sim = sim[:m.start()] + new_sim_langs + sim[m.end():]

    m_ft = re.search(r'function ([A-Za-z0-9_$]+)\(\)\{return [A-Za-z0-9_$]+\}function [A-Za-z0-9_$]+\([A-Za-z0-9_$]+\)\{return typeof', sim)
    ft_fn = m_ft.group(1) if m_ft else None

    m_pl = re.search(
        r'var ([A-Za-z0-9_$]+)=new Intl\.PluralRules\(`pt-BR`\);var ([A-Za-z0-9_$]+)=new Intl\.PluralRules\(`pl`\);'
        r'function ([A-Za-z0-9_$]+)\(([A-Za-z0-9_$]+),([A-Za-z0-9_$]+)\)\{if\(\4===`pl`\)return \2\.select\(\5\);',
        sim
    )
    if not m_pl:
        raise Unsupported('не найдены правила множественного числа в simulation-host.cjs')
    vt_v, ht_v, ut_fn, a1, a2 = m_pl.groups()
    new_pl = (
        f'var {vt_v}=new Intl.PluralRules(`pt-BR`);var {ht_v}=new Intl.PluralRules(`pl`);var RuPl=new Intl.PluralRules(`ru`);'
        f'function {ut_fn}({a1},{a2}){{if({a1}===`ru`)return RuPl.select({a2});if({a1}===`pl`)return {ht_v}.select({a2});'
    )
    sim = sim.replace(m_pl.group(0), new_pl, 1)

    # Scenario staff declension
    m_st = re.search(
        r'=>`\$\{([A-Za-z0-9_$]+)\[([A-Za-z0-9_$]+)\]\} \$\{([A-Za-z0-9_$]+)\(([A-Za-z0-9_$]+)\[\2\]\[\1\[\2\]===1\?0:1\]\)\.toLowerCase\(\)\}`\)\.join\(` · `\)',
        sim
    )
    if m_st:
        iv, ev, tv, ie_v = m_st.groups()
        new_st = (
            f'=>{{const _c={iv}[{ev}];if({tv}(`common.mechanics`)===`Механики`){{'
            f'const _m={{mechanic:[`механик`,`механика`,`механиков`],driver:[`пилот`,`пилота`,`пилотов`],'
            f'designer:[`конструктор`,`конструктора`,`конструкторов`],exec:[`руководитель`,`руководителя`,`руководителей`],'
            f'builder:[`строитель`,`строителя`,`строителей`]}}[{ev}];'
            f'const _f={ut_fn}(`ru`,_c);const _w=_f===`one`?_m[0]:_f===`few`?_m[1]:_m[2];return`${{_c}} ${{_w}}`}}'
            f'return`${{_c}} ${{{tv}({ie_v}[{ev}][_c===1?0:1]).toLowerCase()}}`}}).join(` · `)'
        )
        sim = sim[:m_st.start()] + new_st + sim[m_st.end():]

    if ft_fn:
        sim, _ = replace_optional(sim, [
            ('return i(`A bend handle is too long`)', f'return i({ft_fn}()===`ru`?`Рычаг поворота слишком длинный`:`A bend handle is too long`)'),
            ('return i(`Circuit length must be between the pit straight plus 12 tiles and 2,000 tiles`)', f'return i({ft_fn}()===`ru`?`Длина трассы должна быть от длины пит-прямой + 12 клеток до 2 000 клеток`:`Circuit length must be between the pit straight plus 12 tiles and 2,000 tiles`)'),
            ('return i(`Keep the entire road on owned land`)', f'return i({ft_fn}()===`ru`?`Вся трасса должна находиться на твоей земле`:`Keep the entire road on owned land`)'),
            ('return i(`The circuit overlaps the pit apron`)', f'return i({ft_fn}()===`ru`?`Трасса пересекает пит-лейн`:`The circuit overlaps the pit apron`)'),
            ('return i(`The circuit overlaps a wall`)', f'return i({ft_fn}()===`ru`?`Трасса пересекает стену`:`The circuit overlaps a wall`)'),
            ('return i(`The circuit overlaps ${n.label}`)', f'return i({ft_fn}()===`ru`?`Трасса пересекает объект: ${{n.label}}`:`The circuit overlaps ${{n.label}}`)'),
            ('return i(`The circuit crosses itself or two sections are too close`)', f'return i({ft_fn}()===`ru`?`Трасса пересекает сама себя или участки слишком близко`:`The circuit crosses itself or two sections are too close`)'),
            ('return i(`A bend is too tight for the car; widen the curve or lengthen its handles`)', f'return i({ft_fn}()===`ru`?`Поворот слишком крутой для машины — расширь дугу или вытяни рычаги`:`A bend is too tight for the car; widen the curve or lengthen its handles`)'),
            ('e.toast(`warning`,`Production requires a Head of Production and a finalised in-house chassis design.`)', f'e.toast(`warning`,{ft_fn}()===`ru`?`Для производства нужен глава производства и завершённый проект собственного шасси.`:`Production requires a Head of Production and a finalised in-house chassis design.`)'),
            ('e.toast(`warning`,`Fully test every in-house part before production: performance, durability, failure point and lifespan must all be final.`)', f'e.toast(`warning`,{ft_fn}()===`ru`?`Полностью испытай каждую собственную деталь: скорость, прочность, точка отказа и ресурс должны быть определены.`:`Fully test every in-house part before production: performance, durability, failure point and lifespan must all be final.`)'),
            ('e.toast(`warning`,`Fully test every in-house component before signing off the production specification.`)', f'e.toast(`warning`,{ft_fn}()===`ru`?`Полностью испытай все собственные компоненты перед утверждением спецификации.`:`Fully test every in-house component before signing off the production specification.`)'),
            ('e.toast(`success`,`Domestic certification granted — this chassis and engine can enter domestic production.`)', f'e.toast(`success`,{ft_fn}()===`ru`?`Национальная сертификация получена — это шасси и двигатель могут идти в серию.`:`Domestic certification granted — this chassis and engine can enter domestic production.`)'),
            ('e.toast(`info`,`Circuit plan cancelled with its pit garage; materials refunded`)', f'e.toast(`info`,{ft_fn}()===`ru`?`План трассы отменён вместе с пит-гаражом; материалы возвращены`:`Circuit plan cancelled with its pit garage; materials refunded`)'),
            ('`Pit garage removed — circuit works cancelled`', f'({ft_fn}()===`ru`?`Пит-гараж снесён — строительство трассы отменено`:`Pit garage removed — circuit works cancelled`)'),
            ('`Finish or cancel the current circuit works first`', f'({ft_fn}()===`ru`?`Сначала заверши или отмени текущие работы на трассе`:`Finish or cancel the current circuit works first`)'),
            ('`Finish overlapping construction before planning this circuit`', f'({ft_fn}()===`ru`?`Заверши пересекающуюся стройку перед планированием трассы`:`Finish overlapping construction before planning this circuit`)'),
            ('`Complete the circuit before changing its surface`', f'({ft_fn}()===`ru`?`Заверши постройку трассы перед сменой покрытия`:`Complete the circuit before changing its surface`)'),
            ('`This layout already has the required surface layers`', f'({ft_fn}()===`ru`?`У этой конфигурации уже уложено нужное покрытие`:`This layout already has the required surface layers`)'),
            ('`The test circuit is closed for works`', f'({ft_fn}()===`ru`?`Тестовый трек закрыт на работы`:`The test circuit is closed for works`)'),
            ('`Tier 5 cars require a road test circuit`', f'({ft_fn}()===`ru`?`Машинам уровня 5 требуется асфальтовый тестовый трек`:`Tier 5 cars require a road test circuit`)'),
            ('`Simulator room is not ready — check enclosure, workstations and installed cockpit in Testing.`', f'({ft_fn}()===`ru`?`Зал симулятора не готов — проверь стены, рабочие места и кокпит в разделе «Тесты».`:`Simulator room is not ready — check enclosure, workstations and installed cockpit in Testing.`)'),
            ('`No vehicle in the yard needs that colour right now`', f'({ft_fn}()===`ru`?`На площадке нет машин, которым сейчас нужен этот цвет`:`No vehicle in the yard needs that colour right now`)'),
            ('`A production car still occupies this assembly bay. Move the car to the yard before moving the bay.`', f'({ft_fn}()===`ru`?`В этом сборочном боксе стоит серийная машина. Перемести её на площадку перед переносом бокса.`:`A production car still occupies this assembly bay. Move the car to the yard before moving the bay.`)'),
            ('`Collect the production cars before moving their reserved parking.`', f'({ft_fn}()===`ru`?`Дождись отгрузки серийных машин перед переносом их парковки.`:`Collect the production cars before moving their reserved parking.`)'),
            ('`Collect the production cars before removing their reserved parking.`', f'({ft_fn}()===`ru`?`Дождись отгрузки серийных машин перед удалением их парковки.`:`Collect the production cars before removing their reserved parking.`)'),
            ('`This assembly bay is reserved by a production car. Finish or cancel the batch before removing it.`', f'({ft_fn}()===`ru`?`Этот сборочный бокс занят серийной машиной. Заверши или отмени партию перед удалением.`:`This assembly bay is reserved by a production car. Finish or cancel the batch before removing it.`)'),
            ('`Move vehicles out before changing this parking assignment.`', f'({ft_fn}()===`ru`?`Освободи места от машин перед сменой назначения парковки.`:`Move vehicles out before changing this parking assignment.`)'),
            ('`Choose a chassis model for this bodywork design.`', f'({ft_fn}()===`ru`?`Выбери модель шасси для этого проекта кузова.`:`Choose a chassis model for this bodywork design.`)'),
            ('`The old walls are down`', f'({ft_fn}()===`ru`?`Старые стены снесены`:`The old walls are down`)'),
            ('`Production purchase refunded: programme unavailable`', f'({ft_fn}()===`ru`?`Возврат за серийную закупку: программа недоступна`:`Production purchase refunded: programme unavailable`)'),
            ('`Unused production purchases refunded`', f'({ft_fn}()===`ru`?`Возврат за неиспользованные серийные закупки`:`Unused production purchases refunded`)'),
            ('`Recovered production materials`', f'({ft_fn}()===`ru`?`Возвращённые материалы производства`:`Recovered production materials`)'),
            ('`Pit blueprint cancelled — circuit materials refunded`', f'({ft_fn}()===`ru`?`Чертёж пит-гаража отменён — материалы трассы возвращены`:`Pit blueprint cancelled — circuit materials refunded`)'),
            ('`Circuit works abandoned — materials refunded`', f'({ft_fn}()===`ru`?`Работы на трассе отменены — материалы возвращены`:`Circuit works abandoned — materials refunded`)'),
            ('`Existing circuit road reused — excess materials refunded`', f'({ft_fn}()===`ru`?`Использовано старое полотно — лишние материалы возвращены`:`Existing circuit road reused — excess materials refunded`)'),
            ('`Bank loan drawdown`', f'({ft_fn}()===`ru`?`Выдача банковского кредита`:`Bank loan drawdown`)'),
            ('`Paint job refund`', f'({ft_fn}()===`ru`?`Возврат за покраску`:`Paint job refund`)'),
            ('`Electricity offset (solar + wind)`', f'({ft_fn}()===`ru`?`Компенсация электричества (солнце + ветер)`:`Electricity offset (solar + wind)`)'),
            ('`Wall segment cancelled — materials refunded`', f'({ft_fn}()===`ru`?`Участок стены отменён — материалы возвращены`:`Wall segment cancelled — materials refunded`)'),
            ('`Wall segment blocked — materials refunded`', f'({ft_fn}()===`ru`?`Участок стены заблокирован — материалы возвращены`:`Wall segment blocked — materials refunded`)'),
        ])
    return sim


def patch_main_js(mjs):
    # Find the locale ref imported from releaseGameBridge (used in LanguagePicker as `===`en``)
    m_loc = re.search(r'([A-Za-z0-9_$]+)\(([A-Za-z0-9_$]+)\)===`en`\?.{0,120}`languagePicker\.first_pass_translation_note`', mjs)
    m_imp = re.search(r'([A-Za-z0-9_$]+) as ' + (re.escape(m_loc.group(2)) if m_loc else '__NONE__') + r'\b', mjs[:8000]) if m_loc else None
    if not m_loc or not m_imp:
        return mjs

    orig_export = m_imp.group(1)
    mjs = mjs.replace(f'{orig_export} as {m_loc.group(2)}', f'{orig_export} as {m_loc.group(2)},{orig_export} as __ruLoc', 1)
    helpers = (
        'function _ru(e,r){return __ruLoc.value===`ru`?r:e}'
        'function _ruSt(s){return __ruLoc.value!==`ru`?s:({Fitted:`Установлено`,Staged:`Подготовлено`,Building:`Изготовление`,Collecting:`Сбор`,Waiting:`Ожидание`,Complete:`Готово`,Active:`Идёт оценка`}[s]||s)}'
    )
    mjs = helpers + mjs

    pairs = [
        ('`Parking assignment`', '_ru(`Parking assignment`,`Назначение парковки`)'),
        ('[{value:`general`,label:`General parking`},{value:`production`,label:`Production Only`}]',
         '[{value:`general`,get label(){return _ru(`General parking`,`Общая парковка`)}},{value:`production`,get label(){return _ru(`Production Only`,`Только для серии`)}}]'),
        ('`Close ×`', '_ru(`Close ×`,`Закрыть ×`)'),
        ('`Reserved for`', '_ru(`Reserved for`,`Назначение`)'),
        ('`Production spaces hold completed white cars until collection. Factory lines and roads must connect them to the assembly bays and a loading point.`',
         '_ru(`Production spaces hold completed white cars until collection. Factory lines and roads must connect them to the assembly bays and a loading point.`,`На этих местах хранятся готовые серийные машины в ожидании отгрузки. Заводские линии и дороги должны соединять их со сборочными боксами и точкой погрузки.`)'),
        ('`No Series Partners are available for this series.`', '_ru(`No Series Partners are available for this series.`,`Для этой серии нет доступных партнёров.`)'),
        ('`This is where you can arrange series-approved supply deals when a championship offers them.`',
         '_ru(`This is where you can arrange series-approved supply deals when a championship offers them.`,`Здесь можно оформить одобренные чемпионатом контракты на поставки, если серия их предлагает.`)'),
        ('[{key:`opening`,label:`Opening balance`,group:`in`},{key:`winnings`,label:`Winnings`,group:`in`},{key:`rights`,label:`Commercial rights`,group:`in`},{key:`sponsor`,label:`Sponsorship`,group:`in`},{key:`otherIn`,label:`Other income`,group:`in`},{key:`wages`,label:`Wages`,group:`out`},{key:`services`,label:`Services & retainers`,group:`out`},{key:`travel`,label:`Travel`,group:`out`},{key:`fuel`,label:`Race fuel`,group:`out`},{key:`electricity`,label:`Electricity`,group:`out`},{key:`parts`,label:`Parts & repairs`,group:`out`},{key:`fees`,label:`Entry & supply fees`,group:`out`},{key:`build`,label:`Build & land`,group:`out`},{key:`interest`,label:`Loan interest`,group:`out`},{key:`loans`,label:`Loan draw / repay`,group:`out`},{key:`misc`,label:`Other`,group:`out`}]',
         '[{key:`opening`,get label(){return _ru(`Opening balance`,`Начальный баланс`)},group:`in`},{key:`winnings`,get label(){return _ru(`Winnings`,`Призовые`)},group:`in`},{key:`rights`,get label(){return _ru(`Commercial rights`,`Коммерческие права`)},group:`in`},{key:`sponsor`,get label(){return _ru(`Sponsorship`,`Спонсорство`)},group:`in`},{key:`otherIn`,get label(){return _ru(`Other income`,`Прочие доходы`)},group:`in`},{key:`wages`,get label(){return _ru(`Wages`,`Зарплаты`)},group:`out`},{key:`services`,get label(){return _ru(`Services & retainers`,`Услуги и абонплата`)},group:`out`},{key:`travel`,get label(){return _ru(`Travel`,`Логистика`)},group:`out`},{key:`fuel`,get label(){return _ru(`Race fuel`,`Гоночное топливо`)},group:`out`},{key:`electricity`,get label(){return _ru(`Electricity`,`Электричество`)},group:`out`},{key:`parts`,get label(){return _ru(`Parts & repairs`,`Детали и ремонт`)},group:`out`},{key:`fees`,get label(){return _ru(`Entry & supply fees`,`Взносы и поставки`)},group:`out`},{key:`build`,get label(){return _ru(`Build & land`,`Стройка и земля`)},group:`out`},{key:`interest`,get label(){return _ru(`Loan interest`,`Проценты по кредиту`)},group:`out`},{key:`loans`,get label(){return _ru(`Loan draw / repay`,`Кредит: взятие / возврат`)},group:`out`},{key:`misc`,get label(){return _ru(`Other`,`Прочее`)},group:`out`}]'),
        ('R(`span`,null,`Chassis Mk`+W(e.model.mark),1)', 'R(`span`,null,_ru(`Chassis Mk`,`Шасси Mk`)+W(e.model.mark),1)'),
        ('R(`span`,{class:`required-badge`},`Required`,-1)', 'R(`span`,{class:`required-badge`},_ru(`Required`,`Требуется`),-1)'),
        ('B(`Crash Test Certification `,-1)', 'B(_ru(`Crash Test Certification `,`Краш-тест для сертификации `),-1)'),
        ('B(` chassis ready`,-1)', 'B(_ru(` chassis ready`,` шасси готово`),-1)'),
        ('B(` short`,-1)', 'B(_ru(` short`,` не хватает`),-1)'),
        ('B(` fee available`,-1)', 'B(_ru(` fee available`,` на взнос есть`),-1)'),
        ('`Two chassis of this design are required, stored and unreserved at 90%+ condition.`', '_ru(`Two chassis of this design are required, stored and unreserved at 90%+ condition.`,`Требуются два свободных шасси этого проекта на складе в состоянии 90%+.`)'),
        ('`Each attempt costs ${P(Q)(15e3)}. Available funds include your overdraft.`', '_ru(`Each attempt costs ${P(Q)(15e3)}. Available funds include your overdraft.`,`Каждая попытка стоит ${P(Q)(15e3)}. Учитываются средства овердрафта.`)'),
        ('B(`Chassis Mk`+W(e.model.mark)+` requires Crash Test Certification`,1)', 'B(_ru(`Chassis Mk`,`Шасси Mk`)+W(e.model.mark)+_ru(` requires Crash Test Certification`,` требует краш-теста`),1)'),
        ('B(`Mk`+W(e.model.mark)+` · Crash Test Certification · `+W(e.model.crashStatus===`passed`?`Certified`:e.model.crashStatus===`pending`?`Testing · up to 2 days`:`Not certified`),1)',
         'B(`Mk`+W(e.model.mark)+_ru(` · Crash Test Certification · `,` · Краш-тест · `)+W(e.model.crashStatus===`passed`?_ru(`Certified`,`Сертифицировано`):e.model.crashStatus===`pending`?_ru(`Testing · up to 2 days`,`Идёт тест · до 2 дн.`):_ru(`Not certified`,`Не пройдено`)),1)'),
        ('B(`Requires 2 chassis · `+W(e.model.crashTubsReady)+` ready`,1)', 'B(_ru(`Requires 2 chassis · `,`Нужно 2 шасси · `)+W(e.model.crashTubsReady)+_ru(` ready`,` готово`),1)'),
        ('` · Need `+W(2-e.model.crashTubsReady)+` more`', '_ru(` · Need `,` · Нужно ещё `)+W(2-e.model.crashTubsReady)'),
        ('R(`h3`,null,`Road car designs`,-1)', 'R(`h3`,null,_ru(`Road car designs`,`Проекты дорожных авто`),-1)'),
        ('`Enter Production`', '_ru(`Enter Production`,`К производству`)'),
        ('`+ New car model`', '_ru(`+ New car model`,`+ Новая модель`)'),
        ('`Design road cars and parts for sale. Some series let you race a prepared road car straight away. Others require certification before entry.`',
         '_ru(`Design road cars and parts for sale. Some series let you race a prepared road car straight away. Others require certification before entry.`,`Проектируй дорожные автомобили и детали на продажу. В некоторых сериях на доработанной дорожной машине можно гоняться сразу; другие требуют сертификации.`)'),
        ('W(e),9,Vde', 'W(_ru({Chassis:`Шасси`,"Chassis type":`Тип шасси`,"Model specific":`Для модели`,General:`Общие`}[e]||e,e)),9,Vde'),
        ('`Before designing a part for racing, check the series entry requirements. Some series restrict which WORKS parts you can race.`',
         '_ru(`Before designing a part for racing, check the series entry requirements. Some series restrict which WORKS parts you can race.`,`Перед разработкой детали для гонок проверь регламент серии. Некоторые серии ограничивают допуск деталей WORKS.`)'),
        ('`Hide archived designs`', '_ru(`Hide archived designs`,`Скрыть архив`)'),
        ('`Show archived designs`', '_ru(`Show archived designs`,`Показать архив`)'),
        ('`Parts for this batch`', '_ru(`Parts for this batch`,`Детали для этой партии`)'),
        ('`Programme supply ⓘ`', '_ru(`Programme supply ⓘ`,`Поставки для серии ⓘ`)'),
        ('`Buy in stages as funds allow. Your own designs need fabricating.`', '_ru(`Buy in stages as funds allow. Your own designs need fabricating.`,`Закупай поэтапно по мере наличия средств. Собственные разработки нужно изготовить.`)'),
        ('`Road-car specification`', '_ru(`Road-car specification`,`Спецификация дорожного авто`)'),
        ('`Estimated cost per car`', '_ru(`Estimated cost per car`,`Расчётная себестоимость авто`)'),
        ('`Retail parts`', '_ru(`Retail parts`,`Магазинные детали`)'),
        ('`Your designs`', '_ru(`Your designs`,`Свои разработки`)'),
        ('`Make each`', '_ru(`Make each`,`За 1 шт.`)'),
        ('`Distribution scope`', '_ru(`Distribution scope`,`Масштаб сбыта`)'),
        ('`The signed-off scope is retained for this production run.`', '_ru(`The signed-off scope is retained for this production run.`,`Утверждённый масштаб сохраняется для всей этой партии.`)'),
        ('`View specification →`', '_ru(`View specification →`,`К спецификации →`)'),
        ('`Signed-off specification`', '_ru(`Signed-off specification`,`Утверждённая спецификация`)'),
        ('`These parts are fixed for this batch. Start a new programme to change the specification.`', '_ru(`These parts are fixed for this batch. Start a new programme to change the specification.`,`Эти детали закреплены за партией. Чтобы изменить спецификацию, начни новую программу.`)'),
        ('`View procurement →`', '_ru(`View procurement →`,`К закупкам →`)'),
        ('`Production is complete. Review the certification results or distribution agreement.`', '_ru(`Production is complete. Review the certification results or distribution agreement.`,`Выпуск завершён. Проверь итоги сертификации или договор сбыта.`)'),
        ('`View certification →`', '_ru(`View certification →`,`К сертификации →`)'),
        ('`Distribution type`', '_ru(`Distribution type`,`Тип сбыта`)'),
        ('`Domestic · 6 cars`', '_ru(`Domestic · 6 cars`,`Внутренний · 6 авто`)'),
        ('`Domestic sales and eligible racing series`', '_ru(`Domestic sales and eligible racing series`,`Продажи внутри страны и допуск в подходящие серии`)'),
        ('`Full programme · 26 cars`', '_ru(`Full programme · 26 cars`,`Полная программа · 26 авто`)'),
        ('`Next: Chassis →`', '_ru(`Next: Chassis →`,`Далее: шасси →`)'),
        ('`In-house design required`', '_ru(`In-house design required`,`Требуется собственный проект`)'),
        ('`Next: Specification →`', '_ru(`Next: Specification →`,`Далее: спецификация →`)'),
        ('`Next: Facilities →`', '_ru(`Next: Facilities →`,`Далее: цеха →`)'),
        ('`Production Bays`', '_ru(`Production Bays`,`Сборочные боксы`)'),
        ('`Production parking`', '_ru(`Production parking`,`Парковка серии`)'),
        ('`Production bay needed`', '_ru(`Production bay needed`,`Нужен сборочный бокс`)'),
        ('`Certification programme`', '_ru(`Certification programme`,`Программа сертификации`)'),
        ('`Certification progress`', '_ru(`Certification progress`,`Ход сертификации`)'),
        ('`Your model is approved. Choose a distribution partner to bring it to market.`', '_ru(`Your model is approved. Choose a distribution partner to bring it to market.`,`Модель одобрена! Выбери дистрибьютора для вывода на рынок.`)'),
        ('`PRODUCTION PARTNER`', '_ru(`PRODUCTION PARTNER`,`ПАРТНЁР ПО СБЫТУ`)'),
        ('`Distributed by`', '_ru(`Distributed by`,`Дистрибьютор`)'),
        ('`Domestic certified`', '_ru(`Domestic certified`,`Сертифицировано для страны`)'),
        ('` · Car delivered`', '_ru(` · Car delivered`,` · Машина доставлена`)'),
        ('` · Car dispatched`', '_ru(` · Car dispatched`,` · Машина отправлена`)'),
        ('`Assessment pending`', '_ru(`Assessment pending`,`Ожидает оценки`)'),
        ('e.status===`Complete`?`Complete`', 'e.status===`Complete`?_ru(`Complete`,`Готово`)'),
        ('`Car not yet assigned`', '_ru(`Car not yet assigned`,`Машина ещё не назначена`)'),
        ('title:`Spare Parts`', 'title:G(`inventoryPanel.spare_parts`)||_ru(`Spare Parts`,`Запасные детали`)'),
        ('title:`Written-off Parts`', 'title:_ru(`Written-off Parts`,`Списанные детали`)'),
        ('`Weekend impact`', '_ru(`Weekend impact`,`Итоги уик-энда`)'),
        ('`Incidents`', '_ru(`Incidents`,`Инциденты`)'),
        ('`Parts scrapped`', '_ru(`Parts scrapped`,`Списано деталей`)'),
        ('`Select a session for full timing and incidents`', '_ru(`Select a session for full timing and incidents`,`Выбери сессию, чтобы увидеть хронометраж и инциденты`)'),
        ('`Total sponsor income`', '_ru(`Total sponsor income`,`Всего от спонсоров`)'),
        ('`Session events`', '_ru(`Session events`,`События сессии`)'),
        ('Le=[{value:2,label:`2h before`},{value:6,label:`6h before`},{value:12,label:`12h before`},{value:24,label:`1 day before`}],Re=[{value:0,label:G(`common.off`)},{value:12,label:`12h before`},{value:24,label:`1 day before`},{value:48,label:`2 days before`}]',
         'Le=[{value:2,get label(){return _ru(`2h before`,`За 2 ч`)}},{value:6,get label(){return _ru(`6h before`,`За 6 ч`)}},{value:12,get label(){return _ru(`12h before`,`За 12 ч`)}},{value:24,get label(){return _ru(`1 day before`,`За 1 день`)}}],Re=[{value:0,get label(){return G(`common.off`)}},{value:12,get label(){return _ru(`12h before`,`За 12 ч`)}},{value:24,get label(){return _ru(`1 day before`,`За 1 день`)}},{value:48,get label(){return _ru(`2 days before`,`За 2 дня`)}}]'),
        ('`PRODUCTION · `+W(o.value.bayNumber?`BAY ${o.value.bayNumber}`:`YARD`)',
         '_ru(`PRODUCTION · `,`СЕРИЯ · `)+W(o.value.bayNumber?_ru(`BAY ${o.value.bayNumber}`,`БОКС ${o.value.bayNumber}`):_ru(`YARD`,`ПЛОЩАДКА`))'),
        ('W(o.value.code??`Car ${o.value.number}`)+` · Assembly`',
         'W(o.value.code??_ru(`Car ${o.value.number}`,`Машина ${o.value.number}`))+_ru(` · Assembly`,` · Сборка`)'),
        ('`parts fitted`', '_ru(`parts fitted`,`деталей установлено`)'),
        ('R(`strong`,null,W(e.status),1)', 'R(`strong`,null,W(_ruSt(e.status)),1)'),
        ('`Arrived parts wait here until their required assemblies are fitted. Fitted parts stay on this production car.`',
         '_ru(`Arrived parts wait here until their required assemblies are fitted. Fitted parts stay on this production car.`,`Прибывшие детали ждут здесь, пока не будут установлены нужные узлы. Установленные детали остаются на этой машине.`)'),
        ('`How parts reach the line`', '_ru(`How parts reach the line`,`Как детали попадают на линию`)'),
        ('`Order components separately. Mechanics collect eligible stock automatically and fit it in assembly order.`',
         '_ru(`Order components separately. Mechanics collect eligible stock automatically and fit it in assembly order.`,`Заказывай компоненты по отдельности. Механики сами забирают подходящие детали со склада и ставят их по порядку сборки.`)'),
        ('`Make →`', '_ru(`Make →`,`Сделать →`)'),
        ('`Draw test circuit`', '_ru(`Draw test circuit`,`Начертить тестовый трек`)'),
        ('B(`Surface `,-1)', 'B(_ru(`Surface `,`Покрытие `),-1)'),
        ('`Undo`', '_ru(`Undo`,`Отменить`)'),
        ('`Delete bend`', '_ru(`Delete bend`,`Удалить поворот`)'),
        ('`Start a new loop`', '_ru(`Start a new loop`,`Начать заново`)'),
        ('`Cancel drawing`', '_ru(`Cancel drawing`,`Отмена`)'),
        ('B(` Upgrade Ghost`,-1)', 'B(_ru(` Upgrade Ghost`,` Контур улучшения`),-1)'),
        ('B(` Show floorplan`,-1)', 'B(_ru(` Show floorplan`,` План участка`),-1)'),
        ('W(f.value?`Materials`:`Materials so far`)', 'W(f.value?_ru(`Materials`,`Материалы`):_ru(`Materials so far`,`Материалов пока`))'),
        ('W(e.busy?`Planning…`:`Plan circuit`)', 'W(e.busy?_ru(`Planning…`,`Планируем…`):_ru(`Plan circuit`,`Запланировать трек`))'),
        ('`Builder labour billed separately.`', '_ru(`Builder labour billed separately.`,`Работа строителей оплачивается отдельно.`)'),
        ('`Adjust the route before building`', '_ru(`Adjust the route before building`,`Скорректируй трассу перед постройкой`)'),
        ('`Circuit ready to build`', '_ru(`Circuit ready to build`,`Трасса готова к постройке`)'),
        ('`Join the amber end to complete the circuit`', '_ru(`Join the amber end to complete the circuit`,`Соедини с оранжевой точкой, чтобы замкнуть круг`)'),
        ('`Click the green start dot to begin`', '_ru(`Click the green start dot to begin`,`Нажми на зелёную стартовую точку, чтобы начать`)'),
        ('`Blue: occupied footprint`', '_ru(`Blue: occupied footprint`,`Синий: занятая площадь`)'),
        ('`Yellow: future Road upgrade — buildable until upgraded`', '_ru(`Yellow: future Road upgrade — buildable until upgraded`,`Жёлтый: будущий асфальт — можно строить до улучшения`)'),
        ('s=[{id:`schedule`,label:`Test schedule`},{id:`laps`,label:`Best laps`},{id:`reconstruction`,label:`Reconstruction`}]',
         's=[{id:`schedule`,get label(){return _ru(`Test schedule`,`Расписание тестов`)}},{id:`laps`,get label(){return _ru(`Best laps`,`Лучшие круги`)}},{id:`reconstruction`,get label(){return _ru(`Reconstruction`,`Реконструкция`)}}]'),
        ('`Circuit overview`', '_ru(`Circuit overview`,`Обзор трассы`)'),
        ('`Your proving ground`', '_ru(`Your proving ground`,`Твой полигон`)'),
        ('`Circuit incomplete`', '_ru(`Circuit incomplete`,`Трасса не замкнута`)'),
        ('`Racing-line length`', '_ru(`Racing-line length`,`Длина траектории`)'),
        ('`Complete your circuit`', '_ru(`Complete your circuit`,`Дострой трассу`)'),
        ('`Draw a loop between the garage ends to prepare the track for testing.`',
         '_ru(`Draw a loop between the garage ends to prepare the track for testing.`,`Проведи замкнутое кольцо между въездами гаража, чтобы подготовить трек к тестам.`)'),
        ('`Draw circuit ↗`', '_ru(`Draw circuit ↗`,`Начертить трассу ↗`)'),
        ('`Test programme`', '_ru(`Test programme`,`Тестовая программа`)'),
        ('`Upcoming sessions`', '_ru(`Upcoming sessions`,`Предстоящие сессии`)'),
        ('`Manage schedule ↗`', '_ru(`Manage schedule ↗`,`К расписанию ↗`)'),
        ('`An open calendar`', '_ru(`An open calendar`,`Свободный календарь`)'),
        ('`No sessions booked`', '_ru(`No sessions booked`,`Сессий не запланировано`)'),
        ('`Plan a test programme to compare your cars and develop their performance.`',
         '_ru(`Plan a test programme to compare your cars and develop their performance.`,`Запланируй тесты, чтобы сравнить машины и повысить их характеристики.`)'),
        ('`Timing archive`', '_ru(`Timing archive`,`Архив времён`)'),
        ('`Best clean laps`', '_ru(`Best clean laps`,`Лучшие чистые круги`)'),
        ('`The fastest clean lap for each vehicle. Compare cars on the same layout and surface; spins and incomplete laps are excluded.`',
         '_ru(`The fastest clean lap for each vehicle. Compare cars on the same layout and surface; spins and incomplete laps are excluded.`,`Быстрейший чистый круг каждой машины. Сравнивай на одной конфигурации и покрытии; вылеты и неполные круги не учитываются.`)'),
        ('B(` Include previous layouts and surfaces`,-1)', 'B(_ru(` Include previous layouts and surfaces`,` Включить прошлые конфигурации и покрытия`),-1)'),
        ('`The first lap is still to come`', '_ru(`The first lap is still to come`,`Первый круг ещё впереди`)'),
        ('`Completed test sessions add each car’s best clean lap here.`', '_ru(`Completed test sessions add each car’s best clean lap here.`,`После завершённых тестов сюда попадёт лучший чистый круг каждой машины.`)'),
        ('`View test schedule →`', '_ru(`View test schedule →`,`Расписание тестов →`)'),
        ('`Circuit development`', '_ru(`Circuit development`,`Развитие полигона`)'),
        ('`Shape the next lap`', '_ru(`Shape the next lap`,`Сформируй следующий круг`)'),
        ('`Current construction pass`', '_ru(`Current construction pass`,`Текущий этап стройки`)'),
        ('`Edit the circuit layout`', '_ru(`Edit the circuit layout`,`Изменить конфигурацию`)'),
        ('`Track surface & pit facilities`', '_ru(`Track surface & pit facilities`,`Покрытие трассы и пит-гараж`)'),
        ('`Track surface`', '_ru(`Track surface`,`Покрытие трассы`)'),
        ('`Surface works · materials`', '_ru(`Surface works · materials`,`Дорожные работы · материалы`)'),
        ('`Initial facility`', '_ru(`Initial facility`,`Начальный объект`)'),
        ('`Facility upgrade`', '_ru(`Facility upgrade`,`Улучшение объекта`)'),
    ]
    mjs, _ = replace_optional(mjs, pairs)
    return mjs


def patch_index_html(html):
    m_copy = re.search(r'"pl":\["[^"]+","[^"]+"\]\}', html)
    if m_copy:
        html = html[:m_copy.end() - 1] + ',"ru":["ВЫЙДИ НА СТАРТ","Ранний доступ — в разработке"]}' + html[m_copy.end():]
    old_read = "try { language = localStorage.getItem('raceworks:locale'); } catch (_) {}"
    if old_read not in html:
        raise Unsupported('не найдена инициализация языка в dist/index.html')
    new_read = f"try {{ if (!localStorage.getItem('{MARKER}')) language = 'ru'; else language = localStorage.getItem('raceworks:locale'); }} catch (_) {{}}"
    return html.replace(old_read, new_read, 1)


# ---------------------------------------------------------------- build / install / restore

def build(asar_src, exe_src, asar_tmp, exe_tmp, log):
    ru_src = json.load(open(os.path.join(BASE, 'ru.json'), encoding='utf-8'))

    log('Читаю app.asar...')
    header, orig_hdr_hash, data_start, files = read_asar_index(asar_src)
    if MARKER in read_asar_member(asar_src, data_start, files, 'dist/index.html').decode('utf-8', 'replace'):
        raise Unsupported('исходный app.asar уже русифицирован')

    bridge_paths = [p for p in files if re.fullmatch(r'dist/assets/releaseGameBridge-[^/]+\.js', p)]
    main_paths = [p for p in files if re.fullmatch(r'dist/assets/main-[^/]+\.js', p)]
    sim_path = 'desktop/generated/simulation-host.cjs'
    if len(bridge_paths) != 1 or len(main_paths) != 1 or sim_path not in files:
        raise Unsupported('неожиданная структура файлов внутри app.asar')
    bridge_path, main_path = bridge_paths[0], main_paths[0]

    bridge = read_asar_member(asar_src, data_start, files, bridge_path).decode('utf-8')
    sim = read_asar_member(asar_src, data_start, files, sim_path).decode('utf-8')
    mjs = read_asar_member(asar_src, data_start, files, main_path).decode('utf-8')
    html = read_asar_member(asar_src, data_start, files, 'dist/index.html').decode('utf-8')
    main_cjs = read_asar_member(asar_src, data_start, files, 'desktop/main.cjs').decode('utf-8')

    bridge, ru_literal = patch_bridge(bridge, ru_src, log)
    sim = patch_sim_host(sim, ru_literal)
    mjs = patch_main_js(mjs)
    html = patch_index_html(html)
    main_cjs, _ = replace_optional(main_cjs, [
        ("dialog.showErrorBox('Steam required', steam.error)", "dialog.showErrorBox('Требуется Steam', steam.error)")
    ])

    replacements = {
        bridge_path: bridge.encode('utf-8'),
        sim_path: sim.encode('utf-8'),
        main_path: mjs.encode('utf-8'),
        'dist/index.html': html.encode('utf-8'),
        'desktop/main.cjs': main_cjs.encode('utf-8'),
    }

    log('Пересобираю app.asar (около 700 МБ)...')
    packed = []
    for p, node in files.items():
        if not node.get('unpacked', False) and 'offset' in node:
            packed.append((int(node['offset']), p, node))
    packed.sort(key=lambda x: x[0])

    new_header = copy.deepcopy(header)

    def get_node(hdr, path_str):
        cur = hdr
        for part in path_str.split('/'):
            cur = cur['files'][part]
        return cur

    # First pass: determine new sizes and offsets without holding 700MB in RAM
    cur_offset = 0
    for _, p, orig_node in packed:
        n = get_node(new_header, p)
        n['offset'] = str(cur_offset)
        if p in replacements:
            data = replacements[p]
            n['size'] = len(data)
            n['integrity'] = compute_integrity(data)
            cur_offset += len(data)
        else:
            cur_offset += orig_node['size']

    new_hdr_bytes = json.dumps(new_header, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    new_hlen = len(new_hdr_bytes)
    new_padded_hlen = (new_hlen + 3) & ~3
    pad = b'\x00' * (new_padded_hlen - new_hlen)
    new_prefix = struct.pack('<4I', 4, new_padded_hlen + 8, new_padded_hlen + 4, new_hlen)

    with open(asar_src, 'rb') as src_f, open(asar_tmp, 'wb') as out_f:
        out_f.write(new_prefix)
        out_f.write(new_hdr_bytes)
        out_f.write(pad)
        for orig_off, p, orig_node in packed:
            if p in replacements:
                out_f.write(replacements[p])
            else:
                src_f.seek(data_start + orig_off)
                remaining = orig_node['size']
                while remaining > 0:
                    chunk = src_f.read(min(4194304, remaining))
                    if not chunk:
                        raise Unsupported('неожиданный конец файла app.asar')
                    out_f.write(chunk)
                    remaining -= len(chunk)

    log('Обновляю контроль целостности в WORKS.exe...')
    new_hdr_hash = hashlib.sha256(new_hdr_bytes).hexdigest()
    with open(exe_src, 'rb') as ef:
        exe_data = bytearray(ef.read())
    old_b = orig_hdr_hash.encode('ascii')
    new_b = new_hdr_hash.encode('ascii')
    cnt = exe_data.count(old_b)
    if cnt == 1:
        idx = exe_data.find(old_b)
        exe_data[idx:idx + 64] = new_b
    elif cnt > 1:
        raise Unsupported('неоднозначный хеш ASAR в WORKS.exe')
    with open(exe_tmp, 'wb') as ef:
        ef.write(exe_data)


def install(game, log):
    exe_p = os.path.join(game, EXE_NAME)
    asar_p = os.path.join(game, ASAR_REL)
    exe_orig = exe_p + '.orig'
    asar_orig = asar_p + '.orig'

    st = state(game)
    if st == 'patched':
        if not (os.path.isfile(exe_orig) and os.path.isfile(asar_orig)):
            raise Unsupported(
                'русификация уже установлена, но резервные копии (*.orig) не найдены. '
                'Проверьте целостность файлов игры в Steam и запустите установку снова.'
            )
        log('Русификация уже стоит — переустанавливаю из резервных копий...')
        src_exe, src_asar = exe_orig, asar_orig
    else:
        src_exe, src_asar = exe_p, asar_p

    asar_tmp = asar_p + '.tmp'
    exe_tmp = exe_p + '.tmp'
    try:
        build(src_asar, src_exe, asar_tmp, exe_tmp, log)
        if st == 'clean':
            log('Сохраняю резервные копии (*.orig)...')
            shutil.copyfile(exe_p, exe_orig + '.tmp')
            os.replace(exe_orig + '.tmp', exe_orig)
            shutil.copyfile(asar_p, asar_orig + '.tmp')
            os.replace(asar_orig + '.tmp', asar_orig)
        log('Записываю файлы...')
        os.replace(asar_tmp, asar_p)
        os.replace(exe_tmp, exe_p)
    except Exception:
        for t in (asar_tmp, exe_tmp):
            try:
                if os.path.exists(t):
                    os.remove(t)
            except OSError:
                pass
        raise
    log('Готово! Можно запускать игру.')


def restore(game, log):
    exe_p = os.path.join(game, EXE_NAME)
    asar_p = os.path.join(game, ASAR_REL)
    exe_orig = exe_p + '.orig'
    asar_orig = asar_p + '.orig'

    if state(game) != 'patched':
        log('Игра уже в оригинальном виде.')
        return
    if not (os.path.isfile(exe_orig) and os.path.isfile(asar_orig)):
        raise Unsupported('резервные копии (*.orig) не найдены. Используйте в Steam «Проверить целостность файлов».')
    os.replace(asar_orig, asar_p)
    os.replace(exe_orig, exe_p)
    log('Оригинальные файлы восстановлены.')


# ---------------------------------------------------------------- GUI & CLI

def run_gui():
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    root = tk.Tk()
    root.title('Русификатор WORKS: Motorsport Factory')
    root.geometry('640x430')
    root.resizable(False, False)

    tk.Label(root, text='Папка с игрой (где лежит WORKS.exe):', anchor='w').pack(fill='x', padx=12, pady=(12, 2))
    row = tk.Frame(root)
    row.pack(fill='x', padx=12)
    var = tk.StringVar(value=find_game())
    tk.Entry(row, textvariable=var).pack(side='left', fill='x', expand=True)

    def browse():
        d = filedialog.askdirectory(title='Папка с игрой WORKS: Motorsport Factory')
        if d:
            var.set(os.path.normpath(d))

    tk.Button(row, text='Обзор...', command=browse).pack(side='left', padx=(6, 0))

    log_box = scrolledtext.ScrolledText(root, height=15, state='disabled')
    log_box.pack(fill='both', expand=True, padx=12, pady=10)

    def log(msg):
        def w():
            log_box.configure(state='normal')
            log_box.insert('end', msg + '\n')
            log_box.see('end')
            log_box.configure(state='disabled')
        root.after(0, w)

    btns = tk.Frame(root)
    btns.pack(pady=(0, 12))

    def work(fn, ok_msg):
        game = var.get().strip().strip('"')
        if not is_game_dir(game):
            messagebox.showerror('Русификатор', 'В этой папке не найдены WORKS.exe и resources/app.asar.')
            return
        for b in (b_install, b_restore):
            b.configure(state='disabled')

        def t():
            try:
                fn(game, log)
                root.after(0, lambda: messagebox.showinfo('Русификатор', ok_msg))
            except Unsupported as e:
                log('ОШИБКА: ' + str(e))
                if fn is install:
                    log('Файлы игры не изменены.')
                root.after(0, lambda: messagebox.showerror('Русификатор', str(e)))
            except PermissionError:
                log('ОШИБКА: нет доступа к файлам. Закройте игру и при необходимости запустите от имени администратора.')
                root.after(0, lambda: messagebox.showerror('Русификатор', 'Нет доступа к файлам игры. Закройте игру и повторите.'))
            except Exception as e:  # noqa
                log('НЕОЖИДАННАЯ ОШИБКА: %r' % e)
                root.after(0, lambda: messagebox.showerror('Русификатор', 'Неожиданная ошибка: %r' % e))
            finally:
                root.after(0, lambda: [b.configure(state='normal') for b in (b_install, b_restore)])

        threading.Thread(target=t, daemon=True).start()

    b_install = tk.Button(
        btns, text='Установить русификацию', width=26,
        command=lambda: work(install, 'Русификация установлена. Приятной игры!')
    )
    b_install.pack(side='left', padx=6)
    b_restore = tk.Button(
        btns, text='Вернуть оригинал', width=20,
        command=lambda: work(restore, 'Оригинальные файлы восстановлены.')
    )
    b_restore.pack(side='left', padx=6)

    log('Неофициальный русификатор. Закройте игру перед установкой.')
    log('Файлы игры не входят в программу: она правит ваши собственные файлы.')
    log('Оригиналы сохраняются рядом с суффиксом .orig.')
    if var.get():
        log('Игра найдена: ' + var.get())
    else:
        log('Игра не найдена автоматически — укажите папку вручную.')
    root.mainloop()


def run_launch_mode(exe, args):
    game = os.path.dirname(os.path.abspath(exe))
    try:
        if is_game_dir(game) and state(game) == 'clean':
            install(game, lambda m: None)
    except Exception:  # noqa
        pass
    sys.exit(subprocess.call([exe] + args, cwd=game))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith('.exe') and os.path.isfile(sys.argv[1]):
        run_launch_mode(sys.argv[1], sys.argv[2:])
    elif len(sys.argv) > 2 and sys.argv[1] in ('--install', '--restore'):
        g = sys.argv[2]
        try:
            (install if sys.argv[1] == '--install' else restore)(g, print)
        except Unsupported as e:
            print('ERROR:', e)
            sys.exit(1)
    else:
        run_gui()
