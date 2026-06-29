-- Stäng av Row-Level Security för Sales Chief-verktyget.
-- Detta är ett internt enmansverktyg som körs lokalt med anon-nyckeln —
-- ingen publik åtkomst — så RLS behövs inte och blockerar bara skrivning.
-- Kör hela detta i Supabase SQL Editor (i Sales Chief-projektet), en gång.

ALTER TABLE prospects        DISABLE ROW LEVEL SECURITY;
ALTER TABLE dm_history       DISABLE ROW LEVEL SECURITY;
ALTER TABLE meetings         DISABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs       DISABLE ROW LEVEL SECURITY;
ALTER TABLE agent_log        DISABLE ROW LEVEL SECURITY;
ALTER TABLE lead_suggestions DISABLE ROW LEVEL SECURITY;
ALTER TABLE agent_memory     DISABLE ROW LEVEL SECURITY;
ALTER TABLE inbox_replies    DISABLE ROW LEVEL SECURITY;
