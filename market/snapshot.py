"""
Coleta o Mercado Central do servidor SA (via arsha.io) e gera os arquivos que o
site lê na página "Mercado". Roda a cada 6 horas pelo GitHub Actions
(.github/workflows/market.yml), mas também pode ser rodado à mão:

    python market/snapshot.py

Saídas (pasta market/):
  overview.json   rankings da página central (populares, giro, altas, trajes)
  items.json      índice de todos os itens: reserva quando a API ao vivo falha
  history/        total negociado de cada item por dia (últimos 16 dias)

A arsha.io às vezes recusa pedidos por alguns segundos (bloqueio do servidor da
Pearl Abyss). Cada pedido é repetido com espera; se uma categoria falhar de vez,
os dados anteriores dela são mantidos.
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = 'https://api.arsha.io/v2/sa'
UA = {'User-Agent': 'cabare-da-leila-guias/1.0 (+https://github.com/flipesantos34-cmd/cabaredaleilaguias)'}
OUT = Path(__file__).resolve().parent
HISTORY = OUT / 'history'

# Categorias principais do mercado; as subcategorias são descobertas sozinhas.
MAINS = [1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 65, 70, 75, 80, 85]
OUTFIT_SUBS = {(55, 1), (55, 2), (55, 5)}  # kits de traje (masculino, feminino, gerais)
TOP = 10
HISTORY_CANDIDATES = 300
WINDOW_DAYS = 15
CHANGE_DAYS = 7


def get(path, tries=8):
    url = f'{API}/{path}{"&" if "?" in path else "?"}lang=pt'
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=45) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'ignore')
            if e.code >= 500 or 'Imperva' in body:
                time.sleep(min(60, 5 * (attempt + 1)))
                continue
            return None
        except Exception:
            time.sleep(min(60, 5 * (attempt + 1)))
    return 'ERR'


def as_list(d):
    return d if isinstance(d, list) else [d]


def collect(previous):
    """Lê todas as categorias. Devolve {id: [nome, preço, estoque, total, main, sub]}."""
    items = {}
    for main in MAINS:
        empty = 0
        for sub in range(1, 60):
            d = get(f'GetWorldMarketList?mainCategory={main}&subCategory={sub}')
            if d == 'ERR':
                kept = {k: v for k, v in previous.items() if v[4] == main and v[5] == sub}
                items.update(kept)
                print(f'  {main}-{sub}: falhou, mantidos {len(kept)} itens anteriores', flush=True)
                empty = 0
                continue
            rows = as_list(d) if d else []
            rows = [x for x in rows if isinstance(x, dict) and 'id' in x]
            if not rows:
                empty += 1
                if empty >= 3:
                    break
                continue
            empty = 0
            for x in rows:
                items[str(x['id'])] = [x['name'], x['basePrice'], x['currentStock'], x['totalTrades'], main, sub]
            print(f'  {main}-{sub}: {len(rows)} itens', flush=True)
            time.sleep(0.6)
    return items


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def day_file(day):
    return HISTORY / f'trades-{day.isoformat()}.json'


def trades_since(items, today, days):
    """Vendas no período usando o arquivo diário mais antigo dentro da janela."""
    for back in range(days, 0, -1):
        old = load_json(day_file(today - timedelta(days=back)), None)
        if old:
            return back, {k: max(0, v[3] - old[k]) for k, v in items.items() if k in old}
    return 0, None


def entry(item_id, row, **extra):
    return {'id': int(item_id), 'name': row[0], 'price': row[1], 'stock': row[2], **extra}


def main():
    now = datetime.now(timezone.utc)
    today = now.date()
    HISTORY.mkdir(exist_ok=True)

    previous = load_json(OUT / 'items.json', {}).get('items', {})
    print('Coletando categorias...', flush=True)
    items = collect(previous)
    if len(items) < 500:
        raise SystemExit(f'Poucos itens coletados ({len(items)}); mantendo os arquivos anteriores.')

    # Histórico diário de total negociado (primeira coleta do dia).
    if not day_file(today).exists():
        day_file(today).write_text(json.dumps({k: v[3] for k, v in items.items()}, separators=(',', ':')), encoding='utf-8')
    for f in HISTORY.glob('trades-*.json'):
        if f.stem[7:] < (today - timedelta(days=WINDOW_DAYS + 1)).isoformat():
            f.unlink()

    window, sold = trades_since(items, today, WINDOW_DAYS)
    day_window, sold_day = trades_since(items, today, 1)

    regular = {k: v for k, v in items.items() if v[4] != 55}
    outfits = {k: v for k, v in items.items() if (v[4], v[5]) in OUTFIT_SUBS}

    def ranked_by_sales(pool):
        if sold is not None:
            ranked = sorted(pool, key=lambda k: sold.get(k, 0), reverse=True)
            return [entry(k, pool[k], sold=sold.get(k, 0)) for k in ranked[:TOP] if sold.get(k, 0) > 0]
        ranked = sorted(pool, key=lambda k: pool[k][3], reverse=True)
        return [entry(k, pool[k], sold=pool[k][3]) for k in ranked[:TOP]]

    popular = ranked_by_sales(regular)
    top_outfits = ranked_by_sales(outfits)

    # Giro: vendas em relação ao estoque disponível.
    base = sold_day if sold_day is not None else {k: v[3] for k, v in regular.items()}
    # Só itens com estoque: dividir por zero inflaria o giro dos esgotados.
    turnover = {k: base.get(k, 0) / v[2] for k, v in regular.items() if base.get(k, 0) > 0 and v[2] > 0}
    trending = [entry(k, regular[k], turnover=round(turnover[k], 1)) for k in sorted(turnover, key=turnover.get, reverse=True)[:TOP]]

    # Altas de preço: histórico dos itens mais negociados (os mais líquidos).
    liquid_basis = sold if sold is not None else {k: v[3] for k, v in regular.items()}
    candidates = sorted((k for k in regular if regular[k][1] >= 100), key=lambda k: liquid_basis.get(k, 0), reverse=True)[:HISTORY_CANDIDATES]
    changes = {}
    print(f'Histórico de preço de {len(candidates)} itens...', flush=True)
    for k in candidates:
        d = get(f'GetMarketPriceInfo?id={k}&sid=0', tries=4)
        if not d or d == 'ERR':
            continue
        hist = as_list(d)[0].get('history', {})
        prices = [p for _, p in sorted(hist.items(), key=lambda kv: int(kv[0]))]
        if len(prices) > CHANGE_DAYS and prices[-CHANGE_DAYS - 1] > 0:
            changes[k] = (prices[-1] - prices[-CHANGE_DAYS - 1]) / prices[-CHANGE_DAYS - 1] * 100
        time.sleep(0.4)
    gainers = [entry(k, regular[k], change=round(changes[k], 1)) for k in sorted(changes, key=changes.get, reverse=True)[:TOP] if changes[k] > 0]

    most_sold_id = max(regular, key=lambda k: regular[k][3])
    overview = {
        'updatedAt': now.isoformat(timespec='seconds'),
        'region': 'sa',
        'itemCount': len(items),
        'marketValue': sum(v[1] * v[2] for v in items.values()),
        'mostSold': entry(most_sold_id, regular[most_sold_id], sold=regular[most_sold_id][3]),
        # Dias de histórico usados nas vendas (0 = ainda sem histórico: total acumulado).
        'salesWindowDays': window,
        'turnoverWindowDays': day_window,
        'changeDays': CHANGE_DAYS,
        'popular': popular,
        'trending': trending,
        'gainers': gainers,
        'outfits': top_outfits,
    }
    (OUT / 'overview.json').write_text(json.dumps(overview, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    (OUT / 'items.json').write_text(
        json.dumps({'updatedAt': overview['updatedAt'], 'items': items}, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )
    print(f'Pronto: {len(items)} itens, janela de vendas {window} dias, {len(changes)} históricos.', flush=True)


if __name__ == '__main__':
    main()
