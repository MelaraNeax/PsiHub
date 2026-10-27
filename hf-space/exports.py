"""Exportacion de papers a RIS (Zotero, Mendeley, EndNote) y BibTeX.

RIS es el formato que Zotero importa sin configuracion y arrastra DOI, resumen
y URL; BibTeX es el que espera LaTeX/Overleaf. El navegador puede generar
ambos sin backend, pero se hace aca para que el formato sea consistente y para
que una exportacion de carpeta entera no dependa de la memoria del cliente.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable


def _clean(value: object) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _authors(paper: dict) -> list[str]:
    raw = paper.get('authors') or paper.get('author') or []
    if isinstance(raw, str):
        raw = [part for part in re.split(r';|,(?![^(]*\))', raw) if part.strip()]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for author in raw:
        if isinstance(author, dict):
            name = (author.get('name') or author.get('display_name')
                    or ' '.join(filter(None, (author.get('given'), author.get('family')))))
        else:
            name = author
        clean = _clean(name)
        if clean:
            out.append(clean)
    return out


_NAME_PARTICLES = {'van', 'von', 'de', 'da', 'del', 'der', 'den', 'di', 'la', 'le', 'bin', 'al'}


def _split_name(name: str) -> tuple[str, str]:
    """Devuelve (apellido, nombre) para RIS; conserva partículas y compuestos."""
    if ',' in name:
        surname, _, given = name.partition(',')
        return surname.strip(), given.strip()
    parts = name.split()
    if len(parts) <= 1:
        return name, ''
    start = len(parts) - 1
    while start > 0 and parts[start - 1].lower() in _NAME_PARTICLES:
        start -= 1
    return ' '.join(parts[start:]), ' '.join(parts[:start])


def _year(paper: dict) -> str:
    year = paper.get('year')
    if year:
        return str(year)[:4]
    match = re.search(r'(1[5-9]\d{2}|20\d{2}|21\d{2})', str(paper.get('publicationDate') or ''))
    return match.group(1) if match else ''


def _url(paper: dict) -> str:
    doi = _clean(paper.get('doi'))
    if doi:
        return f'https://doi.org/{doi}'
    return _clean(paper.get('oaUrl') or paper.get('pdfUrl') or paper.get('url'))


def _ris_type(paper: dict) -> str:
    kind = f"{paper.get('type') or ''} {' '.join(paper.get('pubTypes') or [])}".lower()
    if 'meta-analysis' in kind or 'review' in kind:
        return 'JOUR'
    if 'preprint' in kind:
        return 'UNPB'
    if 'book' in kind:
        return 'BOOK'
    return 'JOUR'


def _ris_lines(paper: dict) -> list[str]:
    authors = _authors(paper)
    fields: list[tuple[str, str]] = [('TY', _ris_type(paper))]
    title = _clean(paper.get('title'))
    if title:
        fields.append(('TI', title))
    translated = _clean(paper.get('titleEs'))
    if translated and translated != title:
        fields.append(('T1', translated))
    for author in authors:
        surname, given = _split_name(author)
        fields.append(('AU', f'{surname}, {given}'.strip(', ')))
    year = _year(paper)
    if year:
        fields.append(('PY', year))
    journal = _clean(paper.get('journal'))
    if journal:
        fields.append(('JO', journal))
    if paper.get('volume'):
        fields.append(('VL', _clean(paper.get('volume'))))
    if paper.get('issue'):
        fields.append(('IS', _clean(paper.get('issue'))))
    pages = _clean(paper.get('pages') or '')
    if pages:
        fields.append(('SP', pages))
    doi = _clean(paper.get('doi'))
    if doi:
        fields.append(('DO', doi))
    abstract = _clean(paper.get('abstract'))
    if abstract:
        fields.append(('AB', abstract))
    url = _url(paper)
    if url:
        fields.append(('UR', url))
    publisher = _clean(paper.get('publisher'))
    if publisher:
        fields.append(('PB', publisher))
    if paper.get('pmid'):
        fields.append(('AN', _clean(paper.get('pmid'))))
    fields.append(('ER', ''))
    return [f'{tag}  - {value}'.rstrip() for tag, value in fields]


def to_ris(papers: Iterable[dict]) -> str:
    blocks = ['\n'.join(_ris_lines(paper)) for paper in papers if isinstance(paper, dict)]
    return '\n\n'.join(blocks) + ('\n' if blocks else '')


def _ascii_key(text: str) -> str:
    normalized = unicodedata.normalize('NFKD', text or '')
    ascii_text = normalized.encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^A-Za-z0-9]', '', ascii_text).lower()


def _bib_key(paper: dict, used: set[str]) -> str:
    authors = _authors(paper)
    surname = _split_name(authors[0])[0] if authors else 'psihub'
    base = (_ascii_key(surname) or 'psihub') + _year(paper)
    key = base
    suffix = 0
    while key in used:
        suffix += 1
        key = base + chr(96 + suffix)
    used.add(key)
    return key


def _bib_escape(value: str) -> str:
    return (value.replace('\\', r'\textbackslash{}')
            .replace('&', r'\&').replace('%', r'\%')
            .replace('_', r'\_').replace('#', r'\#'))


def _bib_field(name: str, value: str) -> str:
    clean = _clean(value)
    if not clean:
        return ''
    return f'  {name} = {{{_bib_escape(clean)}}},\n'


def _bib_entry(paper: dict, used: set[str]) -> str:
    kind = _ris_type(paper)
    entry_type = {'JOUR': 'article', 'UNPB': 'misc', 'BOOK': 'book'}.get(kind, 'article')
    authors = _authors(paper)
    author_text = ' and '.join(authors)
    body = (
        _bib_field('title', paper.get('title') or '')
        + _bib_field('author', author_text)
        + _bib_field('year', _year(paper))
        + _bib_field('journal', paper.get('journal') or '')
        + _bib_field('volume', paper.get('volume') or '')
        + _bib_field('number', paper.get('issue') or '')
        + _bib_field('pages', paper.get('pages') or '')
        + _bib_field('doi', paper.get('doi') or '')
        + _bib_field('abstract', paper.get('abstract') or '')
        + _bib_field('url', _url(paper))
        + _bib_field('publisher', paper.get('publisher') or '')
    ).rstrip(',\n')
    return f'@{entry_type}{{{_bib_key(paper, used)},\n{body}\n}}\n'


def to_bibtex(papers: Iterable[dict]) -> str:
    used: set[str] = set()
    entries = [_bib_entry(paper, used) for paper in papers if isinstance(paper, dict)]
    return '\n'.join(entries)
