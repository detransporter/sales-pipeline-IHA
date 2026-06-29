# 🧠 Sales Chief — orchestratorn

En agent som håller ihop hela sälj-flödet åt dig, så du bara behöver **utföra**, inte planera.
Den bygger ovanpå dina befintliga agenter (prospecting → dm_generator → followup → qualifier)
och lägger till tre nya: **learning** (vad funkar), **lead_finder** (nya leads) och
**orchestrator** (chefen som koordinerar och loggar allt).

## Vad den gör varje körning
1. **Läser läget** — pipeline från Supabase
2. **Lär sig** — räknar ut vilken DM-vinkel och bransch som ger flest svar/möten
3. **Förbereder jobbet** — genererar färdiga DM:s för nya kontakter (med lärdomarna inbakade),
   plockar fram uppföljningar som förfallit, föreslår nya leads
4. **Loggar allt** — till Supabase (`agent_runs`, `agent_log`) och till Open Brain (minnet)
5. **Sammanfattar** — en prioriterad lista som kan puttas till Telegram

Supabase = strukturerad sanning. **Det berättande minnet ligger också i din egen Supabase**
(tabellen `agent_memory`) — ingen Open Brain-koppling behövs. Allt är tåligt: saknas Telegram
körs allt ändå.

> Vill du i stället använda Open Brain som minne: i `agents/orchestrator.py`, byt raden
> `from brain import memory as brain` till `from brain import open_brain as brain`.
> Inget annat behöver ändras — båda har exakt samma gränssnitt.

## Aktivera — 3 steg

### 1. Kör den nya databas-SQL:en (en gång)
Öppna Supabase → SQL Editor → klistra in och kör hela `database/schema.sql`.
De nya raderna (orchestrator-lagret) är `IF NOT EXISTS`/`ADD COLUMN IF NOT EXISTS`,
så det är ofarligt att köra om — inget förstörs.

### 2. Testa i appen
```
cd /Users/davidleifsson/sales/linkedin_dm_agent
streamlit run app.py
```
Gå till sidan **🧠 Sales Chief** → tryck **▶️ Kör dagen**.
Du ser förberedda DM:s, uppföljningar och nya leads att godkänna.

### 3. Schemalägg morgonkörningen (valfritt men rekommenderat)
Kör automatiskt 07:30 varje vardag. I terminalen: `crontab -e` och lägg in raden:
```
30 7 * * 1-5 cd /Users/davidleifsson/sales/linkedin_dm_agent && /usr/bin/python3 run_daily.py >> orchestrator.log 2>&1
```
Då får du en färdig prioriteringslista i Telegram varje morgon (om `TELEGRAM_BOT_TOKEN`
och `TELEGRAM_CHAT_ID` finns i `.env`).

## Köra från Telegram
Starta boten (`python telegram/bot.py`) och skriv **/chef** när du vill — då kör Sales Chief
direkt och svarar med dagens lista i chatten.

## Köra manuellt i terminalen
```
python run_daily.py
```

## 💬 Inkorg-agent — fångar svar åt dig (read-only)

En agent som läser dina LinkedIn-svar, matchar dem mot rätt kontakt, kvalificerar svaret
och **förbereder ditt nästa meddelande** — men skickar aldrig något själv. Du öppnar appen
(sidan **💬 Inkorg**), läser och trycker skicka. Ingen inklistring, inget missas.

### Koppla LinkedIn via Unipile (en gång)
1. Skapa konto på **unipile.com** → anslut ditt LinkedIn-konto i deras dashboard
2. Hämta tre värden därifrån: **DSN** (t.ex. `apiXXX.unipile.com:13XXX`), **API-nyckel**,
   och **Account ID** för ditt LinkedIn-konto
3. Lägg in dem i `.env`:
   ```
   UNIPILE_DSN=apiXXX.unipile.com:13XXX
   UNIPILE_API_KEY=din-nyckel
   UNIPILE_ACCOUNT_ID=ditt-linkedin-konto-id
   ```
4. Kör den nya databas-SQL:en (skapar `inbox_replies`) + `database/disable_rls.sql` i Supabase

### Köra inkorg-kollen
- **I appen:** sidan 💬 Inkorg → "🔄 Kolla inkorgen nu"
- **Telegram:** `/inkorg`
- **Automatiskt var 15:e minut** (cron):
  ```
  */15 7-20 * * 1-5 cd /Users/davidleifsson/sales/linkedin_dm_agent && /usr/bin/python3 run_inbox.py >> inbox.log 2>&1
  ```
  Då pingar den dig på Telegram så fort någon svarat.

> **Säkerhet:** agenten är **read-only** och skickar aldrig meddelanden automatiskt — det är
> det säkraste för ditt LinkedIn-konto. Unipile bryter tekniskt mot Linkedins villkor (som alla
> sådana verktyg), men med läsning och vettiga intervall är fotavtrycket litet.

## 🔎 Research-agent — riktiga bolag via Apify (gratis)

Tidigare gissade lead-finder bolag ur Claudes minne. Nu hittar den **verkliga** svenska
bolag via Apifys Google Maps-scraper — utan att röra LinkedIn eller ditt konto (helt säkert).
Claude väljer sedan de bästa IHA-kandidaterna ur den verkliga listan och annoterar dem.

### Koppla Apify (en gång)
1. Skapa gratis konto på **apify.com** (ingen betalning, $5 krediter/månad)
2. Gå till **Settings → Integrations** → kopiera din **API-token**
3. Lägg in i `.env`:
   ```
   APIFY_TOKEN=apify_api_din_token
   ```
4. Kör den nya `database/schema.sql` igen (lägger till `website`/`source`/`samtal_steg`-kolumner — ofarligt)

Sedan: på **🧠 Sales Chief** ser du "Research-läge: Apify". Skriv ev. ett **sök-fokus**
(t.ex. *"livsmedelstillverkare i Mälardalen"*) och kör dagen. Saknas token faller den
automatiskt tillbaka på AI-gissning. **Tips:** håll volymerna måttliga — $5/mån räcker långt
på Google Maps om du kör riktade sökningar.

## 🏢 Hitta bolag (ekonomi) — Allabolag-screener

Den vassaste tratt-toppen: hitta bolag där kapital **bevisligen** sitter fast i lager,
direkt ur Allabolags publika siffror. **Helt gratis** — söker mot Allabolags egen
bransch+ort-sökning (en hämtning ger ~25 bolag med omsättning/resultat/anställda),
förfiltrerar på storlek/lönsamhet, och hämtar bara detaljsidan (varulager) på de få
som är värda det. Ingen betald actor, inga Apify-krediter, ingen LinkedIn.
Sidan **🏢 Hitta bolag (ekonomi)**:

Två metoder (radioknapp överst):

**🔎 Auto-sök (Segmentering):** välj **bransch** (lager-tunga SNI-branscher) + **län**. Appen
filtrerar Allabolags **Segmentering** server-side på omsättning + plats (gratis att läsa —
hundratals bolag), behåller vald bransch via SNI-koden, drar ifrån bolag med för hög marginal,
och läser sedan **varulager** per bolag för att räkna lagerandel. Resultat: en rankad lista med
riktiga IHA-kandidater. Allt gratis (ingen betald export behövs — vi läser listan, inte köper den).

**📋 Egen lista:** klistra in **org-nummer** (ett per rad) eller ladda upp en **CSV** (t.ex. en
Segmentering-export). Appen läser varje bolags omsättning, varulager och resultat exakt och
screenar med tydligt skäl när ett bolag faller bort. Org-nr funkar med både gratis och Plus.

> Tips: lagerandel-filtret fungerar som ett automatiskt branschfilter — tjänste-/konsultbolag har
> ~0 varulager och faller bort av sig själva. Sveper du flera län får du fler kandidater.
2. Ställ in filtren (standard = dina IHA-kriterier):
   - Omsättning 50–300 MSEK · Max 200 anställda
   - **Lagerandel > 20%** (varulager / omsättning) — kärnsignalen
   - Vinstmarginal < 3% (pressad lönsamhet)
3. **🔎 Screena bolag** → en lista rankad på **IHA-score** (mest bundet kapital + svagast
   lönsamhet först), med oms, anställda, lagerandel, marginal och orgnr
4. **💾 Spara kvalificerade som leads** → de hamnar i pipeline och kan kontaktas via
   🧠 Sales Chief (🔍 Hitta person → DM → säljtrappa)

> **Aktivera:** kör de nya kolumnerna i `database/schema.sql` (orgnr, omsattning, varulager,
> lagerandel m.fl. — alla `IF NOT EXISTS`, ofarliga). Annars går det inte att spara leads.

### 👤 People finder — hitta rätt person på bolaget

På varje föreslaget lead finns en **🔍 Hitta person**-knapp. Den letar i två lager:

1. **Bolagets hemsida** (gratis, ingen LinkedIn) — om-oss/kontakt/team-sidor
2. **Google → publika LinkedIn-profiler** (fallback) — söker t.ex.
   `"Bolag" (inköpschef OR logistikchef) site:linkedin.com/in`. Skrapar **Google, inte
   LinkedIn**, och använder **aldrig ditt konto** → ingen kontorisk. Kostar lite Apify-krediter.

Claude väger ihop källorna och pekar ut **en** person (namn + roll + ev. LinkedIn-URL) med en
säkerhetsnivå. Den hittar aldrig på personer — saknas tydlig träff säger den det rakt ut.
**Du verifierar alltid profilen på LinkedIn innan kontakt.** Tips: kör knappen bara på de leads
du faktiskt vill kontakta, så sparar du krediter.

## 💬 Samtalsmotor — säljtrappan som leder till möte

Inkorg-agenten skriver inte längre bara ett snällt svar — den läser **hela samtalet**, avgör
var i säljtrappan kontakten är och skriver nästa meddelande som flyttar dem **ett steg**:

`ny → öppning → upptäcker behov → bygger förtroende → erbjuder gratis Inventory Health Snapshot
→ mot bokat samtal → möte bokat`

Dörröppnaren är medvetet låg tröskel: en **kostnadsfri snabbtitt på deras lagerdata**. Motorn
pitchar aldrig för tidigt och hoppar aldrig över ett steg. Du ser vilket steg varje samtal är
på direkt i 💬 Inkorg.

## 🌐 Personliga första-DM via hemsidan

Om ett lead har en hemsida (kommer automatiskt med Apify-leads) hämtar appen kort text därifrån
så att första-meddelandet kan anknyta naturligt till vad bolaget faktiskt tillverkar — utan att
någonsin nämna lager eller sälj. Helt publik data, ingen kontorisk.

## Så blir den smartare över tid
Varje skickat DM sparas med vilken **vinkel** (a/b/c) det använde. När du markerar utfall
(svar ja / möte / nej) kopplar `learning`-agenten ihop vinkel + bransch med resultatet och
matar tillbaka det i DM-genereringen. Ju mer du använder den, desto bättre vinklar väljer den.
Du ser statistiken under "📈 Vad funkar?" på Sales Chief-sidan.
