# ItaliaTV Channels Editor

Editor web per gestire catalogo film, canali sorgente e diagnostiche del repository `italiatv-channels`.

## Registro modifiche

### 2026-06-27
- Aggiunta in **Diagnostica** la voce **Non in italiano / solo sottotitoli**.
- La diagnostica rileva film sub-ITA/non doppiati usando marker come `sub ita`, `sottotitoli in italiano`, `film coreano completo`, `vostit` e dati annidati in `strict_id` / `strict_id_v2`.
- Aggiunto il pulsante **Elimina tutti questi film** nella nuova categoria diagnostica; i film del catalogo vengono marcati `is_movie=false` con motivazione `sub-ita: audio non italiano / solo sottotitoli italiani`.
- Resi visibili nella finestra **Impostazioni** i campi Token GitHub, Chiave Gemini API, Chiave OpenAI API e password di sincronizzazione.

## Regola operativa

Ogni modifica pubblicata all'editor o ai file gestiti dal repository deve aggiornare questo README con una riga nel registro modifiche.
