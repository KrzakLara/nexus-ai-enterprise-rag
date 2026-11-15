# NEXUS AI — kako ga pokrenuti

Verzija v0.2.0 portfolio projekta za Applied AI / AI Engineer pozicije (lokalni demo, nije production-ready).

## Najlakše: Docker Desktop

1. Instaliraj i pokreni Docker Desktop.
2. Kloniraj repozitorij i otvori njegovu mapu u VS Codeu.
3. U terminalu, u glavnoj mapi projekta, pokreni:

   ```bash
   docker compose up --build
   ```

4. Otvori **http://localhost:5173**.
5. Odaberi `finance@northstar.demo` (lozinka `DemoPass123!`).
6. Postavi pitanje: `When does the Atlas supplier agreement expire?`
7. Otvori izvor u desnom panelu, pregledaj ugovor, a zatim promijeni korisnika na `hr@northstar.demo` i probaj ponovno.

**API dokumentacija:** http://localhost:8000/docs

## Bez Dockera (Python + Node)

U terminalu 1, iz glavne mape:

```powershell
pip install uv
uv sync --locked --extra dev
.venv\Scripts\Activate.ps1
python scripts/seed_demo.py --skip-embeddings   # SQLite mod: samo bm25
uvicorn backend.app.main:app --reload
```

U terminalu 2:

```powershell
cd frontend
npm ci
npm run dev
```

Zatim otvori http://localhost:5173.

## Prvi testovi

```bash
python -m pytest -q        # bez PostgreSQL-a: 168 prolazi, 52 preskočeno (s PostgreSQL-om svih 220 prolazi)
python scripts/evaluate_retrieval.py
```

**API ključ nije potreban.** Embeddingi se rade lokalno (`intfloat/multilingual-e5-small`; prvi start Dockera preuzima ~470 MB). Ispod chata odaberi način pretraživanja: *BM25 (baseline)*, *Keyword · PostgreSQL FTS*, *Semantic · pgvector* ili *Hybrid · RRF* (zadano na PostgreSQL-u).

Probaj:

- `NS-IT-2026-017`: točan identifikator. Keyword ga nalazi, a Hybrid ga stavlja na prvo mjesto.
- `Koliko dana unaprijed moramo najaviti raskid ugovora s dobavljačem?`: parafraza. BM25 i Keyword ne nalaze ništa. Hybrid odgovara doslovnim citatom rečenice iz Atlas ugovora (*"…at least 90 days before the current end date."*). Sličnost te rečenice (0,857) prelazi prag 0,835, kalibriran na dev splitu. U Phase 2c isto je pitanje bilo lažno odbijeno (sličnost cijelog chunka 0,810 < 0,82). Korisnik bez pristupa (`hr@northstar.demo`) ne dobiva tekst Atlas ugovora.
- Izbornik **Evaluation** prikazuje stvarne rezultate posljednjeg pokretanja `python scripts/evaluate.py`.

RRF score je statistika rangova, **nije** pouzdanost ni vjerojatnost.

Korisne naredbe:

```bash
python scripts/migrate.py                 # Alembic migracije
python scripts/embed_backfill.py          # embeddingi za postojeće chunkove (idempotentno)
```

Ako želiš LLM-generirane odgovore, postavi `OPENAI_API_KEY` prema README-u. Evaluacija (100 označenih pitanja, Recall@k, MRR, nDCG, abstencija, citati, sigurnost) opisana je u `docs/evaluation.md`. Još nisu napravljeni query translation, LLM evaluacija odgovora, reranking, OCR i production SSO.

**Bitno:** Ovo je lokalni demo sa sintetičkim dokumentima. Nije namijenjen stvarnim povjerljivim podacima ni javnom deploymentu.
