-- LinkedIn DM Agent — Supabase Schema
-- Kör detta i Supabase SQL Editor

-- Kontakter
CREATE TABLE IF NOT EXISTS prospects (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    namn TEXT NOT NULL,
    titel TEXT,
    bolag TEXT,
    bransch TEXT,
    linkedin_url TEXT,
    telefon TEXT,
    extra_info TEXT,
    score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ej_kontaktad' CHECK (status IN (
        'ej_kontaktad', 'skickad', 'followup_1', 'followup_2',
        'svar_ja', 'svar_nej', 'inget_svar', 'mote_bokat'
    )),
    created_at TIMESTAMP DEFAULT NOW()
);

-- DM-historik
CREATE TABLE IF NOT EXISTS dm_history (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    prospect_id UUID REFERENCES prospects(id) ON DELETE CASCADE,
    meddelande TEXT NOT NULL,
    typ TEXT CHECK (typ IN ('initial', 'followup_1', 'followup_2')),
    status TEXT DEFAULT 'genererad' CHECK (status IN (
        'genererad', 'skickad', 'svar_ja', 'svar_nej', 'inget_svar', 'mote_bokat'
    )),
    skickad_at TIMESTAMP,
    svar_at TIMESTAMP,
    svar_text TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Möten
CREATE TABLE IF NOT EXISTS meetings (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    prospect_id UUID REFERENCES prospects(id) ON DELETE CASCADE,
    datum DATE,
    status TEXT DEFAULT 'bokad' CHECK (status IN ('bokad', 'genomford', 'avbokad')),
    anteckningar TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Index för vanliga queries
CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
CREATE INDEX IF NOT EXISTS idx_prospects_score ON prospects(score DESC);
CREATE INDEX IF NOT EXISTS idx_dm_history_prospect ON dm_history(prospect_id);
CREATE INDEX IF NOT EXISTS idx_dm_history_status ON dm_history(status);


-- ════════════════════════════════════════════════════════════════════════
-- ORCHESTRATOR-LAGER (kör detta för att aktivera Sales Chief)
-- ════════════════════════════════════════════════════════════════════════

-- Vilken "vinkel" (variant) ett DM använde — gör att vi kan lära oss vad som funkar
ALTER TABLE dm_history ADD COLUMN IF NOT EXISTS angle TEXT;

-- En körning av orchestratorn (morgonkörning eller manuell knapp)
CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    run_type TEXT DEFAULT 'daily' CHECK (run_type IN ('daily', 'manual')),
    started_at TIMESTAMP DEFAULT NOW(),
    finished_at TIMESTAMP,
    summary JSONB,            -- planen + resultatet som JSON
    created_at TIMESTAMP DEFAULT NOW()
);

-- Logg över varje åtgärd en agent gjorde (full spårbarhet — "vad har hänt")
CREATE TABLE IF NOT EXISTS agent_log (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    run_id UUID REFERENCES agent_runs(id) ON DELETE CASCADE,
    agent TEXT NOT NULL,      -- 'orchestrator' | 'prospecting' | 'dm_generator' | 'followup' | 'qualifier' | 'lead_finder' | 'learning'
    action TEXT NOT NULL,     -- kort beskrivning av åtgärden
    prospect_id UUID REFERENCES prospects(id) ON DELETE SET NULL,
    detail JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Nya leads som lead_finder föreslår — David godkänner innan de blir prospects
CREATE TABLE IF NOT EXISTS lead_suggestions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    namn TEXT,
    titel TEXT,
    bolag TEXT NOT NULL,
    bransch TEXT,
    linkedin_url TEXT,
    motivering TEXT,          -- varför detta är en bra IHA-kandidat
    score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Eget minne (ersätter Open Brain) — agenternas berättande långtidsminne
CREATE TABLE IF NOT EXISTS agent_memory (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    content TEXT NOT NULL,
    tags TEXT,                -- valfria etiketter, t.ex. 'orchestrator,pipeline'
    created_at TIMESTAMP DEFAULT NOW()
);

-- Inkommande LinkedIn-svar (read-only inkorg-agent) — kö över svar att hantera
CREATE TABLE IF NOT EXISTS inbox_replies (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    prospect_id UUID REFERENCES prospects(id) ON DELETE CASCADE,
    sender_name TEXT,
    sender_url TEXT,
    text TEXT,
    received_at TIMESTAMP,
    kategori TEXT,            -- kvalificerarens kategori (INTRESSERAD/BOKA_MOTE/...)
    suggested_reply TEXT,     -- färdigt förslag på Davids nästa meddelande
    handled BOOLEAN DEFAULT FALSE,
    external_id TEXT,         -- meddelande-/chatt-id från Unipile (för dedup)
    run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Research-lager (Apify): spara bolagets hemsida och varifrån leadet kom.
-- Ofarligt att köra om (IF NOT EXISTS).
ALTER TABLE lead_suggestions ADD COLUMN IF NOT EXISTS website TEXT;
ALTER TABLE lead_suggestions ADD COLUMN IF NOT EXISTS source TEXT;   -- 'apify' | 'ai'
ALTER TABLE prospects        ADD COLUMN IF NOT EXISTS website TEXT;

-- Samtalsmotor: var i säljtrappan kontakten är, och förslaget steg per svar.
ALTER TABLE prospects     ADD COLUMN IF NOT EXISTS samtal_steg TEXT DEFAULT 'ny';
ALTER TABLE inbox_replies ADD COLUMN IF NOT EXISTS steg TEXT;

-- Ekonomisk screener (Allabolag): finansiella nyckeltal per bolag.
ALTER TABLE lead_suggestions ADD COLUMN IF NOT EXISTS orgnr         TEXT;
ALTER TABLE lead_suggestions ADD COLUMN IF NOT EXISTS omsattning    NUMERIC;   -- MSEK
ALTER TABLE lead_suggestions ADD COLUMN IF NOT EXISTS resultat      NUMERIC;   -- MSEK
ALTER TABLE lead_suggestions ADD COLUMN IF NOT EXISTS anstallda     INTEGER;
ALTER TABLE lead_suggestions ADD COLUMN IF NOT EXISTS varulager     NUMERIC;   -- MSEK
ALTER TABLE lead_suggestions ADD COLUMN IF NOT EXISTS lagerandel    NUMERIC;   -- %
ALTER TABLE lead_suggestions ADD COLUMN IF NOT EXISTS vinstmarginal NUMERIC;   -- %
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS orgnr         TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS omsattning    NUMERIC;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS resultat      NUMERIC;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS anstallda     INTEGER;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS varulager     NUMERIC;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS lagerandel    NUMERIC;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS vinstmarginal NUMERIC;

CREATE INDEX IF NOT EXISTS idx_agent_log_run ON agent_log(run_id);
CREATE INDEX IF NOT EXISTS idx_lead_suggestions_status ON lead_suggestions(status);
CREATE INDEX IF NOT EXISTS idx_agent_memory_created ON agent_memory(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_replies_handled ON inbox_replies(handled);
CREATE INDEX IF NOT EXISTS idx_inbox_replies_external ON inbox_replies(external_id);

-- Sparad bolagssökning — söksidans pool + djuplästa bolag som EN rad (id=1),
-- så att sökningen överlever omstart och djupläsningen kan fortsätta senare.
CREATE TABLE IF NOT EXISTS screen_sessions (
    id INTEGER PRIMARY KEY DEFAULT 1,
    pool JSONB NOT NULL DEFAULT '[]',
    fins JSONB NOT NULL DEFAULT '[]',
    read_count INTEGER NOT NULL DEFAULT 0,
    funnel JSONB NOT NULL DEFAULT '{}',
    label TEXT DEFAULT '',
    updated_at TIMESTAMP DEFAULT NOW()
);
