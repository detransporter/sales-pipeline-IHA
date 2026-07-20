"""
Central modellkonfiguration — ETT ställe att ändra Claude-modell för hela appen.

Innan detta låg MODEL = "..." hårdkodat separat i sju filer, med tre olika
modellgenerationer i drift utan att någon fil förklarade varför. Vill du byta
modell för en användning bytte du på fel ställe, eller missade ett.

Nu: en namngiven konstant per användningsfall, med anledning. Alla agenter
importerar härifrån istället för att hårdkoda sin egen sträng.
"""

# Rutinuppgifter med strukturerad JSON-output: lead-sök, samtalsmotor,
# DM-generering, kvalificering av inkommande svar. Bra balans mellan
# kvalitet och kostnad för väldefinierade uppgifter.
MODEL_STANDARD = "claude-sonnet-4-6"

# Kundvänt innehåll där kvalitet märks direkt hos mottagaren: mejlutkast,
# bolagets IHA-föranalys.
MODEL_QUALITY = "claude-sonnet-5"

# Dagcoach-chatten (David Agent) — öppna, resonerande samtal snarare än
# strukturerad output. Motiverar en starkare modell än rutinagenterna.
MODEL_COACH = "claude-opus-4-6"

# Enkel klassificering/textläsning utan djupare resonemang: affärsmodell-
# klassning, personläsning på hemsidetext. Billigast och snabbast.
MODEL_FAST = "claude-haiku-4-5"
