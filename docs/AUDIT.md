# Audit de conformité — NTFAL (Elder Triple Screen × Hyperliquid)

- **Date** : 2026-10-08 · **Base auditée** : `c32ab4b` (branche `claude/eloquent-pascal-7ic5oa`)
- **Périmètre** : `indicators/`, `strategy/`, `risk/`, `data/`, `app/pipeline.py`, `config.toml`, et la
  spec canonique de `CLAUDE.md`.
- **Source de vérité** : *The New Trading for a Living* (Elder, 2014), fichier
  `The New Trading for a Living.pdf` **à la racine du dépôt** (le chemin `docs/new_trading_for_a_living.pdf`
  indiqué dans la mission n'existe pas ; rien n'a été déplacé).

## 0. Méthode

**Pagination.** `p. N` désigne la **page imprimée** du livre ; `(PDF M)` l'index de page dans le fichier.
Le décalage est constant : **PDF = imprimée + 16**. Pages lues (imprimées) : 74–89, 112–117, 124–131,
154–172, 202–213, 215–229, 238–242.

**État de départ** (avant toute modification) : `pytest` → **114 passed** ; `ruff check .` → OK ;
`black --check .` → OK (29 fichiers).

**Vérifications en direct.** Un script jetable placé **hors du dépôt** a interrogé l'endpoint public
`info` le 2026-10-08 vers 12:00 UTC : `meta` (natif et `dex="xyz"`), `candleSnapshot` (6 coins × 6
intervalles), `clearinghouseState` (natif et `dex="xyz"`), et une exécution complète de
`build_snapshot` avec un cache temporaire. `recentTrades` a servi uniquement à trouver une adresse
publique détenant une position `xyz`, pour vérifier le format du payload (l'adresse n'est pas
consignée). Aucun test du dépôt n'appelle le réseau.

**Verdicts.** `conforme` · `écart justifié` (adaptation défendable, documentée) · `erreur` (le code
ne fait pas ce que le livre, la spec ou sa propre docstring disent — corrigée, avec un test qui échoue
avant et passe après) · `à trancher` (choix de design ou de calibrage : **non tranché**, voir §4).

---

## 1. Volet 1 — Conformité au livre

### 1.1 Triple Screen

| # | Point | Livre (page) | Code (fichier:ligne) | Verdict | Action |
|---|---|---|---|---|---|
| T1 | Trois écrans : tide (long terme, décision stratégique), wave (oscillateur contre la vague), entry (technique d'entrée) | p.155–158 (PDF 171–174) | `strategy/triple_screen.py:1-21`, `:601-683` | conforme | — |
| T2 | Facteur ~5 entre timeframes | p.125 (PDF 141) : « approximately 5 » (4,5 sem./mois, 5 j/sem., 5–6 h/j) ; p.156 (PDF 172) ; p.162 (PDF 178) | `config.toml:33-62` — swing 1w/1d/4h = ×7 et ×6 (barres 24/7) ; scalp 4h/1h/15m = ×4, ×4 ; micro 1h/15m/5m = ×4, ×3 | écart justifié | Hyperliquid n'offre que des intervalles fixes ; pour le 3e écran Elder laisse « quite a bit of latitude », jusqu'au même timeframe que le wave (p.158). |
| T3 | Premier écran = pente d'une EMA du tide / Impulse | p.156–157 (PDF 172–173) : « slope of a weekly exponential moving average… I began to use [Impulse] for the first screen » ; p.115 (PDF 131) : tendance « identified by the slope of a 13-day EMA » ; p.80 (PDF 96, légende) : « the slow EMA helps identify the trend » | `triple_screen.py:92-113`, `:662-667` | écart justifié | Le livre n'impose pas 13 vs 26 pour l'écran 1 ; EMA13 = l'EMA « inertie » de l'Impulse (p.162) et celle citée p.115. |
| T4 | Marché sans tendance : EMA plate ⇒ pas de système de tendance | p.77 (PDF 93), règle 3 | `triple_screen.py:92-113` ; `strategy/params.py:18` (`flat_trend_slope_pct = 0.001`) | conforme (principe) · **à trancher** (calibrage) | Seuil en %/barre, non invariant selon le timeframe. Mesuré en direct : tide 1h « neutre » 89 % (GOLD), 99 % (SP500), 62 % (CL) du temps ; tide 4h 63 / 89 / 33 %. L'horizon micro est quasi muet. → **Q1** |
| T5 | 2e écran : FI(2) < 0 en tendance haussière ⇒ achat ; > 0 en baissière ⇒ vente ; sinon rester dehors | p.158 (PDF 174) ; tableau p.160 (PDF 176) ; p.113–115 (PDF 129–131) | `triple_screen.py:635-658` | conforme | — |
| T6 | Réserve : pas de signal si FI(2) fait un nouveau plus bas (haut) multi-semaines | p.158 (PDF 174) « as long as it doesn't fall to a new multi-week low » ; p.115 (PDF 131) « lowest low in a month » | `triple_screen.py:137-157`, `:638-656` ; `params.py:23` (25 barres) | conforme | 25 barres 1d (24/7) ≈ 3,5 semaines. |
| T7 | Censure Impulse : rouge interdit l'achat, vert la vente, sur l'hebdo **ou** le quotidien (écrans 1 et 2) ; le 3e écran ne censure jamais | p.163 (PDF 179) ; p.157 (PDF 173) ; p.164 (PDF 180) | `triple_screen.py:660-667` ; tests `test_third_screen_impulse_never_vetoes_*` | conforme | — |
| T8 | Acheter dans/sous la zone de valeur, pas au-dessus (veto directionnel) | p.79–80 (PDF 95–96) ; Trade Apgar p.240 (PDF 256) : au-dessus de la valeur 0, dans la zone 1, sous la valeur 2 | `triple_screen.py:160-194`, `:668-683` ; `params.py:35` (3 %) | conforme (principe directionnel) · **à trancher** (calibrage) | Tolérance fixe de 3 % : mesurée en direct, jamais dépassée sur 1h / 15m (0 %, 0 %, 0,6 % des barres) ⇒ filtre inactif sur scalp/micro ; actif sur swing (GOLD 14 %, CL 57 % des barres 1d). → **Q2** |
| T9 | Entrée en breakout : buy-stop 1 tick au-dessus du plus haut de la barre précédente / sell-stop sous le plus bas ; peut se prendre sur un timeframe plus court | p.158 (PDF 174), p.161 (PDF 177) | `triple_screen.py:332-338`, `:354-360`, `:500-519` | conforme | Tick : voir H10. |
| T10 | Ordre limite à « EMA de demain − pénétration moyenne » sur 4–6 semaines ; EMA projetée = EMA + (EMA − EMA veille) | p.159–160 (PDF 175–176), Fig. 39.3 | `triple_screen.py:116-134`, `:291-296`, `:341`, `:363` ; `params.py:19` (35 barres) | conforme (projection, lookback) · **à trancher** (mesure) | Le code moyenne la pénétration de **chaque barre** sous l'EMA ; Elder mesure **une pénétration par repli** (4 occasions A–D, Fig. 39.3). Les barres d'entrée/sortie d'un repli diluent la moyenne ⇒ limite trop proche. → **Q3** |
| T11 | Si non exécuté, abaisser le buy-stop chaque jour au plus haut de la dernière barre + 1 tick, jusqu'à exécution ou retournement de l'indicateur hebdo | p.161 (PDF 177) ; p.115 (PDF 131) | `triple_screen.py:271-288` ; `params.py:41` (`entry_order_expire_bars = 2`) | **à trancher** | Expiration après 2 barres absente du livre ; « roll to the latest high + 1 tick » peut **monter** l'ordre alors qu'Elder dit « lower ». → **Q4** |
| T12 | Objectif sur le tide (zone de valeur hebdo), stop sur le wave ; au-delà de la valeur, objectif au canal | p.162 (PDF 178) ; p.171 (PDF 187) ; p.218 (PDF 234) | `triple_screen.py:345-350`, `:367-370` | conforme | — |
| T13 | Récompense/risque ≥ 2:1 | p.154 (PDF 170) ; p.216 (PDF 232) | `params.py:27` ; `triple_screen.py:731` ; `app/pipeline.py:356-371` | conforme | « Signaler sans masquer » est un choix d'outil, cohérent avec p.131 (PDF 147 : objectifs plus petits en day-trading). |
| T14 | Choisir le meilleur trade | Trade Apgar p.238–242 (PDF 254–258) : 5 questions notées 0/1/2, seuil ≥ 7 et aucun zéro ; pour un achat, Impulse hebdo/quotidien **bleu après rouge = 2**, vert = 1, rouge = 0 ; p.164 (PDF 180) : « the best trading signals are given not by green or red but by the loss of green or red » | `triple_screen.py:537-598` (0,40 R:R · 0,25 Impulse **vert** · 0,20 pente tide · 0,15 profondeur FI) | **à trancher** | Le score n'est pas celui d'Elder et récompense l'inverse de l'Apgar sur l'Impulse ; la docstring « Elder's "which setup?" criteria » est inexacte. N'affecte que le classement, jamais une action. → **Q5** |

### 1.2 Indicateurs et paramètres

| # | Point | Livre (page) | Code (fichier:ligne) | Verdict | Action |
|---|---|---|---|---|---|
| I1 | EMA, K = 2/(N+1) | p.76 (PDF 92) | `indicators/ema.py:8-12` | conforme | Test golden existant. |
| I2 | Paire EMA 13/26 (~2:1), zone de valeur entre les deux | p.79 (PDF 95) | `triple_screen.py:39-40` | conforme | — |
| I3 | MACD 12/26, signal 9 ; histogramme = MACD − signal | p.81 (PDF 97), p.83 (PDF 99) | `indicators/macd.py:10-13` | conforme | — |
| I4 | Pente de MACD-H = relation des deux dernières barres, signe indifférent | p.84 (PDF 100), p.163 (PDF 179) | `indicators/impulse.py:28-31` | conforme | — |
| I5 | Force Index = Volume × (C − C₋₁) ; EMA(2) pour les entrées, EMA(13) pour le contexte | p.112–113 (PDF 128–129) | `indicators/force_index.py:10-13` ; `triple_screen.py:447` | conforme | — |
| I6 | Impulse : EMA↑ et MACD-H↑ vert ; ↓↓ rouge ; mixte bleu | p.162–163 (PDF 178–179), Fig. 40.1 | `indicators/impulse.py:15-31` | conforme | 1re barre bleue : convention (pas de pente). |
| I7a | Canal parallèle à l'EMA **lente** (26), largeur = coefficient × EMA | p.167 (PDF 183) | `triple_screen.py:299-329` | conforme | — |
| I7b | Le canal contient ~95 % des prix | p.79 (PDF 95) et p.167 (PDF 183) : ~95 % ; p.226 (PDF 242) : « between 90% and 95% of prices for the past 100 bars » | `triple_screen.py:321-328` | **erreur** | Un quantile 0,95 **par côté** laisse ~5 % au-dessus **et** ~5 % en dessous. Mesuré en direct : **84,6 %** de barres contenues sur les 6 actifs (1w, 4h, 1h), sous la fourchette d'Elder ; la docstring affirme à tort « leaves ~(1−containment) of bars outside ». → **C3** |
| I7c | Fenêtre de 100 barres ; un coefficient symétrique | p.79 (PDF 95), p.167 (PDF 183) | `params.py:24` (26 barres) ; `triple_screen.py:323-328` (deux demi-largeurs) | **à trancher** | 26 barres ≠ 100 ; l'asymétrie est un choix documenté du code. → **Q6** |
| I8 | SafeZone : bruit = partie de la barre qui dépasse le plus bas (haut) précédent ; moyenne sur 10–20 jours ; ×2 ou plus pour les longs, ×3 pour les shorts | p.220 (PDF 236) | `triple_screen.py:197-247` ; `params.py:36-40` (20 barres, 2, 3) | conforme | Le livre est ambigu (p.220 écrit aussi « downside penetrations of the EMA ») : le code suit la définition du bruit du paragraphe précédent et la version short (« previous bars' highs »), ce qui se défend. Ancrage sur le min des 2 dernières barres au lieu de la veille : plus prudent, justifié. |

### 1.3 Divergences

| # | Point | Livre (page) | Code (fichier:ligne) | Verdict | Action |
|---|---|---|---|---|---|
| D1 | L'indicateur doit **traverser** sa ligne zéro entre les deux extrêmes | p.87 (PDF 103) : « breaking of the centerline between two indicator bottoms is an absolute must… has to cross above that line before skidding to its second bottom » ; p.117 (PDF 133), FI(13) : « must make a new peak, then fall below its zero line, and then rise above that line again » | `triple_screen.py:416`, `:428` | **erreur** | Le test est inclusif des extrémités : si l'indicateur est déjà du « mauvais » côté au premier extrême (ex. MACD-H > 0 au premier creux de prix), la condition est vraie sans aucun croisement ⇒ fausse divergence affichée. → **C1** |
| D2 | Extrêmes espacés de 20 à 40 barres | p.88 (PDF 104), Lovvorn | `triple_screen.py:417`, `:429` ; `params.py:29-30` | conforme | Mesuré entre pivots de prix (approximation acceptable). |
| D3 | Divergences MACD-H et FI(13) = avertissements uniquement | p.86–88 (PDF 102–104), p.113 (PDF 129), p.117 (PDF 133) | `triple_screen.py:435-454` | conforme | — |
| D4 | Meilleurs signaux quand le 2e extrême ≤ moitié du 1er | p.89 (PDF 105) | — | non implémenté | Raffinement facultatif, pas un écart. |

### 1.4 Gestion du risque

| # | Point | Livre (page) | Code (fichier:ligne) | Verdict | Action |
|---|---|---|---|---|---|
| R1 | Règle des 2 % ; risquer moins est encouragé | p.203–204 (PDF 219–220) | `risk/sizing.py:8-9`, `:47-48` | conforme | Défaut 1 %, plafond dur 2 %. |
| R2 | Iron Triangle : A (risque $) / B (risque par unité) ; < 1 unité ⇒ pas de trade | p.204–206 (PDF 220–222) | `sizing.py:30-61` | conforme | Taille arrondie vers le bas ; taille 0 = pas de trade. |
| R3 | La limite de 2 % se calcule sur l'équité du 1er du mois | p.204 (PDF 220) : « Measure your account equity on the first day of each month » | `app/pipeline.py:363-370` (`cfg.risk.equity`) | **à trancher** | Le dimensionnement utilise l'équité courante. → **Q7** |
| R4 | Règle des 6 % : pertes du mois + risque ouvert ≥ 6 % de l'équité de début de mois ⇒ plus de nouvelle entrée | p.208 (PDF 224) | `sizing.py:64-74` ; `pipeline.py:429-444` | conforme | Garde globale, calculée une fois pour tous les horizons. |
| R5 | Risque ouvert = (entrée − stop courant) × taille, **nul** dès que le stop est au point mort ou au-delà | p.208 (PDF 224) : « if you … move your stop to breakeven, your open risk will become zero » ; p.209 (PDF 225) : « nothing in stock A, because its stop is above breakeven » | `pipeline.py:212-215` (`abs(entry − suggested_stop)`) | **erreur** | Un stop au-delà du point mort (profit verrouillé) est compté comme du risque ⇒ peut déclencher la règle des 6 % à tort. → **C2** |
| R6 | Cibles : zone de valeur du tide, ou canal | p.162, p.171, p.218 | voir T12 | conforme | — |
| R7 | Stops sur le wave, hors du bruit (SafeZone) | p.162 (PDF 178), p.220 (PDF 236) | voir I8 | conforme | — |
| R8 | Plusieurs horizons = plusieurs « comptes » | p.126 (PDF 142) : « consider making those trades in different accounts » | avertissement d'empilement dans l'UI et le CLI | conforme | — |

### 1.5 Sorties (positions ouvertes)

| # | Point | Livre (page) | Code (fichier:ligne) | Verdict | Action |
|---|---|---|---|---|---|
| X1 | Sortir quand la tendance (pente de l'EMA13) se retourne | p.115 (PDF 131) ; p.161 (PDF 177) | `strategy/trade_management.py:221-224` | conforme | — |
| X2 | Ne jamais rester contre la couleur : long + rouge sur un des deux timeframes ⇒ sortir ; short + vert ⇒ couvrir | p.166 (PDF 182) | `trade_management.py:225-231` | conforme | — |
| X3 | Prendre ses profits à l'objectif (zone de valeur / canal) | p.162, p.171, p.216–218 | `trade_management.py:133-148`, `:235-240` | conforme | — |
| X4 | Perte de la couleur favorable | p.164 (PDF 180) : un momentum trader sort dès qu'**un** timeframe passe au bleu ; p.166 (PDF 182) : « a swing trader may stay in a trade, even if one of the timeframes turns blue » | `trade_management.py:241-250` (les **deux** sans couleur favorable **et** en profit) | écart justifié | Version « swing » (positions_horizon = swing) ; la condition « en profit » est un ajout prudent. |
| X5 | Trailing stop SafeZone, ramené au point mort en profit | p.77 (PDF 93) : « move it to the break-even point as soon as prices close higher » ; p.115 (PDF 131) « as early as possible » ; p.223 (PDF 239) : à un niveau de profit planifié | `trade_management.py:151-174` | conforme | Conforme à p.77/p.115 ; p.223 propose une variante plus patiente. |
| X6 | Ne bouger le stop que dans le sens du trade | p.224 (PDF 240) | `trade_management.py:151-174` (calcul sans mémoire) | **à trancher** | D'un rafraîchissement à l'autre, la suggestion peut baisser (long) : Elder l'interdit. → **Q8** |

### 1.6 Documentation

| # | Point | Livre (page) | Code (fichier:ligne) | Verdict | Action |
|---|---|---|---|---|---|
| DOC1 | Références de pages | — | `CLAUDE.md:113-114`, `config.toml:84,88`, `strategy/params.py:25,29`, `strategy/triple_screen.py:147,305,322,394-395,413-414`, `tests/test_triple_screen.py:184,206,326` | **erreur** (documentaire) | p.103, p.104, p.131, p.183 sont des index **PDF** (imprimées : 87, 88, 115, 167) alors que p.158 et p.220 sont des pages imprimées. → **C8** (commentaires seulement : aucun test possible). |

---

## 2. Volet 2 — Adéquation à Hyperliquid

| # | Point | Constat en direct (2026-10-08) | Code (fichier:ligne) | Verdict | Action |
|---|---|---|---|---|---|
| H1 | Les 6 coins existent dans `meta` du dex `xyz`, noms préfixés, non delisted | `meta` `dex=xyz` : 132 perps dont 18 delisted (URANIUM, ALUMINIUM, DXY, VIX, CORN, WHEAT, …) ; xyz:GOLD, SILVER, CL, BRENTOIL, SP500, XYZ100 présents, **aucun** `isDelisted` ; le `meta` natif ne contient pas GOLD | `data/hyperliquid.py:120-149` | conforme | — |
| H2 | La validation d'un coin **explicite** rejette un perp delisted | Les delisted restent dans `meta` avec `isDelisted: true` | `hyperliquid.py:136-149` | **erreur** | Un coin explicite delisted passe la validation (seuls les jokers l'excluent), contrairement à `CLAUDE.md` (« delisted assets are excluded ») et à la docstring (« any coin that is not a tradable perp »). → **C5** |
| H3 | Résolution multi-dex (une requête `meta` par dex) | `meta dex=xyz` renvoie `xyz:GOLD` déjà préfixé | `hyperliquid.py:51-54`, `:143-149` ; `pipeline.py:152-170` | conforme | — |
| H4 | Limite de 5000 bougies | L'API renvoie les ~5000 plus **récentes** (5002–5027 observées) quel que soit `startTime` ; profondeur : 5m ≈ 17 j, 15m ≈ 52 j, 1h ≈ 208 j | `hyperliquid.py:22-23`, `:226-230` | conforme | Note : un cache plus vieux que ~5000 barres laisserait un trou (sans effet sur les valeurs finales, les EMA ayant reconvergé sur 5000 barres neuves). |
| H5 | Intervalles 5m, 15m, 1h, 4h, 1d, 1w | Tous répondent pour les 6 coins | `hyperliquid.py:28-42` ; `config.py:143-148` | conforme | Note : les bougies **1w commencent le jeudi 00:00 UTC** (alignement epoch), donc la barre hebdo va de jeudi à mercredi. À documenter (→ Q12). |
| H6 | Champs string → float | `o/h/l/c/v` sont des chaînes ; `T − t` = intervalle − 1 ms | `hyperliquid.py:57-84`, `:250-255` | conforme | — |
| H7 | Cache parquet | `:` remplacé dans le nom de fichier | `hyperliquid.py:204-247` | conforme | — |
| H8 | Historique suffisant par horizon | 1w : GOLD 43, SILVER 42, CL 41, BRENTOIL 33, SP500 31, XYZ100 53 barres (< 60) ; 1d : 205–361 ; 4h : 1224–2160 ; 1h, 15m, 5m : ≥ 4895 | `config.toml` (`min_tide_bars = 60`) ; `triple_screen.py:480-485` | conforme | L'avertissement « historique court » se déclenche sur **swing pour les 6 actifs** (vérifié via le pipeline live) ; scalp (1000 barres 4h) et micro (2000 barres 1h) ont assez d'historique. |
| H9 | Avertissement « marché quasi gelé » (week-end) | GOLD 1h : volume médian sam./dim. ≈ 35 contre ≈ 360 en semaine (≈ 10 %), amplitude 0,02–0,03 % contre 0,3 % | `triple_screen.py:487-496` | **erreur** | La référence est la médiane des **60** dernières barres du wave (le paramètre des divergences, réutilisé) : après ~30 barres de week-end, la référence **est** le volume du week-end et l'alerte s'éteint. Rejeu sur 21 j × 6 actifs : alerte sur **196/3456** barres 15m de week-end (micro) et **298/864** barres 1h (scalp). Avec une référence de 1000 barres : 1455/3456 et 397/864, et 4× moins d'alertes en semaine en 15m (le reste correspond au samedi matin, quand la fenêtre de 6 barres contient encore le vendredi). → **C4** |
| H10 | Tick : ≤ 5 chiffres significatifs, ≤ 6 − szDecimals décimales, **entiers toujours admis** (règle Hyperliquid pour les perps) | Pour les 6 coins, la contrainte des 5 chiffres significatifs est active (vérifié sur les chaînes brutes) : GOLD 0,1 ; SILVER 0,001 ; CL 0,001 ; BRENTOIL 0,01 (0,001 sous 100) ; SP500 0,1 ; XYZ100 1 | `triple_screen.py:41-42`, `:85-89` | conforme pour la watchlist par défaut · **erreur latente** | Prix ≥ 100 000 ⇒ tick 10 au lieu de 1 (ex. BTC) ; le plafond 6 − szDecimals est ignoré (actifs bon marché à szDecimals élevé, atteignables via `"*"` / `"xyz:*"`). → **C6** |
| H11 | Arrondi de l'entrée / du stop / de la limite / de l'objectif au tick | Les entrées breakout tombent sur la grille ; stop, limite et objectif ne sont pas arrondis | `triple_screen.py:332-371` | **à trancher** | → **Q9** |
| H12 | Taille arrondie à `szDecimals` | szDecimals : GOLD 4, SILVER 2, CL 3, BRENTOIL 2, SP500 3, XYZ100 4 | `sizing.py:51-52` ; `pipeline.py:363-370` | conforme | Pas de contrôle du notionnel minimum d'un ordre Hyperliquid (10 USD). → **Q10** |
| H13 | Levier max | maxLeverage : GOLD 25, SILVER 25, CL 20, BRENTOIL 20, SP500 50, XYZ100 30 ; certains perps xyz sont `onlyIsolated` | non utilisé | **à trancher** | L'Iron Triangle peut proposer un notionnel > maxLeverage × équité (stops serrés sur micro). Aujourd'hui, le seul setup dimensionné (SP500 swing) demande 0,9× l'équité. → **Q10** |
| H14 | Coûts de funding | `clearinghouseState` contient `cumFunding` par position (endpoint autorisé) ; les taux courants exigent `metaAndAssetCtxs` (hors des 3 endpoints autorisés par `CLAUDE.md`) | non traité | **à trancher** | → **Q11** |
| H15 | Positions lues par dex | `clearinghouseState` `dex=xyz` renvoie `xyz:GOLD`, `xyz:JPY` (préfixés, `szi` en chaîne) ; l'appel natif ne les contient pas | `pipeline.py:178-209` ; `trade_management.py:102-130` | conforme | — |
| H16 | Un échec sur un dex ne fait pas perdre les autres | Docstring : « a failure on one dex doesn't drop the others » | `pipeline.py:200-206` (ne capture que `HyperliquidError`) | **erreur** | Une erreur HTTP ou un timeout `httpx` sur un dex fait échouer **tout** le rafraîchissement (`build_snapshot` ne capture que `HyperliquidError`). → **C7** |
| H17 | Barres gelées du week-end dans des séries 24/7 | Les perps tradfi impriment des barres le week-end (volume et amplitude ≈ 10 %) | — | **à trancher** | EMA13 en 1d ≈ 9 séances + 4 barres gelées ; Elder raisonne en séances (p.125 : 5 jours/semaine). → **Q12** |

---

## 3. Corrections effectuées (erreurs uniquement)

Pour chaque correction, le test a été écrit d'abord et vérifié en **échec** sur le code d'origine,
puis la correction minimale l'a fait passer. Les commits sont listés dans l'historique de la branche.

| Id | Écart | Correction | Test (échoue avant / passe après) |
|---|---|---|---|
| C1 | D1 — croisement de la ligne zéro | Exiger que l'indicateur soit du bon côté de zéro au premier extrême (négatif pour un creux, positif pour un sommet), pour que le croisement ait lieu entre les deux | `test_divergence_needs_a_real_zero_line_cross_not_an_endpoint` |
| C2 | R5 — risque ouvert au-delà du point mort | Risque = max(0, entrée − stop) × taille pour un long ; max(0, stop − entrée) × taille pour un short | `test_open_risk_is_zero_once_the_stop_locks_in_profit` |
| C3 | I7b — contenance du canal | Répartir le budget hors canal sur les deux bords : quantile 1 − (1 − c)/2 par côté | `test_channel_contains_about_95_percent_of_bars` |
| C4 | H9 — avertissement week-end | Référence de volume dédiée, `low_volume_baseline_bars = 1000` barres du wave, découplée de `divergence_lookback` | `test_data_warning_survives_a_whole_frozen_weekend` |
| C5 | H2 — coin explicite delisted | `validate_watchlist` refuse un perp `isDelisted` comme un coin inconnu | `test_validate_watchlist_rejects_explicit_delisted_coin` |
| C6 | H10 — tick Hyperliquid | Entiers toujours admis (tick ≤ 1) ; plafond 6 − szDecimals quand `szDecimals` est connu (transmis par le pipeline) | `test_tick_size_follows_hyperliquid_price_rules`, `test_entry_tick_respects_sz_decimals_cap` |
| C7 | H16 — isolation des échecs par dex | `fetch_open_positions` capture aussi `httpx.HTTPError` par dex | `test_positions_survive_a_failing_dex` |
| C8 | DOC1 — pages citées | Pages imprimées partout (p.87, p.88, p.115, p.167) ; commentaires uniquement | — (aucun comportement modifié) |

---

## 4. Points à trancher — recommandations (non appliquées)

| Id | Sujet | Constat | Recommandation |
|---|---|---|---|
| Q1 | `flat_trend_slope_pct` par horizon (T4) | Médiane de \|pente EMA13\| par barre : 1w 0,25–0,62 % ; 1d 0,12–0,58 % ; 4h 0,04–0,18 % ; 1h 0,015–0,07 %. Avec 0,1 %, le tide 1h est « neutre » 89–99 % du temps (GOLD/SP500). | Surcharge par horizon dans `[scanner.horizons.strategy]`, de l'ordre du 10e–25e centile observé (ex. scalp `0.0002`, micro `0.0001`, swing inchangé). C'est une simple modification de config. |
| Q2 | `value_zone_max_distance_pct` par horizon (T8) | 3 % n'est jamais atteint sur 1h/15m ⇒ le veto « chasing » ne sert que sur swing. | Surcharge par horizon (ex. scalp `0.005`, micro `0.002`), ou tolérance exprimée en fraction de la hauteur du canal du wave (mesure propre à Elder, pas un nouvel indicateur). |
| Q3 | Pénétration moyenne par barre ou par repli (T10) | Elder : une valeur par repli (Fig. 39.3) ; code : chaque barre. | Prendre la pénétration maximale de chaque épisode contigu sous l'EMA, puis faire la moyenne des épisodes. |
| Q4 | Cycle de vie de l'ordre stop (T11) | Expiration après 2 barres et « roll » pouvant monter le buy-stop : absents du livre. | Pas d'expiration fixe (annuler quand le tide ou le 2e écran s'invalide) et ne jamais remonter un buy-stop (ni baisser un sell-stop) ; ou garder ce comportement en le documentant comme choix d'outil. |
| Q5 | Score de classement vs Trade Apgar (T14) | Pondération maison, Impulse vert récompensé ; l'Apgar récompense le bleu après rouge. | Remplacer par un Apgar « pullback to value » (Impulse tide, Impulse wave, prix vs valeur, R:R, profondeur du repli ; 0/1/2 chacun, ≥ 7 et aucun zéro pour être « A-trade ») ; à défaut, corriger la docstring. |
| Q6 | Canal : 100 barres et coefficient symétrique (I7c) | Avec 100 barres + quantile par côté corrigé : 90,5–95,1 % (1w), 94 % (4h/1h). Swing n'a que 31–53 barres hebdo. | `channel_lookback_bars = 100` (valeur du livre ; utilise tout l'historique disponible si plus court) ; trancher symétrique (Elder) vs asymétrique (choix actuel). |
| Q7 | Base de la règle des 2 % (R3) | Elder fige la limite sur l'équité du 1er du mois. | Dimensionner sur `equity_at_month_start`, ou sur `min(equity, equity_at_month_start)` (prudent). |
| Q8 | Stop qui recule (X6) | La suggestion est recalculée à chaque passage, sans mémoire. | Afficher « ne jamais baisser un stop existant » à côté de la suggestion ; l'outil ne connaît pas le stop réel (ordres non lus). |
| Q9 | Arrondi des niveaux au tick (H11) | Stop, limite et objectif non arrondis. | Arrondir dans le sens prudent : buy-stop ↑, sell-stop ↓, stop long ↓, stop short ↑, limite d'achat ↓, limite de vente ↑, objectif vers l'entrée ; dimensionner sur le stop arrondi. |
| Q10 | Levier max et notionnel minimum (H12, H13) | Non contrôlés. | Signaler (pas plafonner silencieusement) quand taille × entrée > maxLeverage × équité, ou < 10 USD ; `maxLeverage` est déjà dans la réponse `meta`. |
| Q11 | Funding (H14) | Non traité. | Afficher `cumFunding.sinceOpen` des positions ouvertes (endpoint déjà autorisé) ; ajouter les taux via `metaAndAssetCtxs` impose d'amender la liste d'endpoints de `CLAUDE.md`. |
| Q12 | Séances vs 24/7 pour les perps tradfi (H5, H17) | Barres gelées du week-end dans les EMA ; barres hebdo jeudi→mercredi. | Au minimum, documenter les deux faits dans le README ; option lourde : ignorer les barres hors séance avant le calcul des indicateurs. |
| Q13 | Règle d'Elder « jamais acheter au-dessus du canal supérieur ni vendre sous l'inférieur » | p.168 (PDF 184). Non implémentée ; le veto de la zone de valeur la couvre en partie. | Optionnel (réutilise le canal existant, pas un nouvel indicateur). |

---

## 5. Bilan

- **Points vérifiés** : 60 (Triple Screen 14, indicateurs 10, divergences 4, risque 8, sorties 6,
  documentation 1, Hyperliquid 17).
- **Conformes ou écarts justifiés** : 42, dont 3 conformes sur le principe mais à recalibrer (T4, T8,
  T10).
- **Erreurs** : 8, toutes corrigées (C1–C8), dont une latente pour la watchlist par défaut (C6) et une
  purement documentaire (C8).
- **À trancher** : 9 points sans verdict, 1 raffinement facultatif non implémenté (D4) ; au total
  13 questions (Q1–Q13). Les deux plus urgentes sont Q1 et Q2 : avec la config actuelle, les horizons
  scalp et micro ne produisent presque jamais de signal, et leur veto de zone de valeur est inactif.
