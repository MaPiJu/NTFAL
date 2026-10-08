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
`black --check .` → OK (29 fichiers). **Après corrections** : **123 passed** ; ruff et black OK.
**Lot 2** (Q2, Q3, Q4, Q7, Q9, Q10, Q11 phase 1, Q13 — voir §3 bis) : de 124 à **141 passed** ;
ruff et black OK à chaque commit. **Lot 3** (Q5, Q6, Q8, Q11 phase 2, Q12 — voir §3 ter) : de 141 à
**166 passed** ; ruff et black OK à chaque commit.
Les références `fichier:ligne` des tableaux renvoient au commit audité `c32ab4b`.

**Vérifications en direct.** Un script jetable placé **hors du dépôt** a interrogé l'endpoint public
`info` le 2026-10-08 vers 12:00 UTC : `meta` (natif et `dex="xyz"`), `candleSnapshot` (6 coins × 6
intervalles), `clearinghouseState` (natif et `dex="xyz"`), et une exécution complète de
`build_snapshot` avec un cache temporaire. `recentTrades` a servi uniquement à trouver une adresse
publique détenant une position `xyz`, pour vérifier le format du payload (l'adresse n'est pas
consignée). Aucun test du dépôt n'appelle le réseau.

**Vérifications en direct, lot 2** (2026-10-08, même méthode : script jetable hors du dépôt).
`candleSnapshot` (6 coins × 1d / 1h / 15m, ≤ 5000 barres) pour recompter Q2 et Q3 sur le code
modifié. Signe de `cumFunding` (Q11) : `recentTrades` pour trouver des adresses publiques
détenant des positions `xyz`, puis `clearinghouseState` et `userFunding` pour comparer
(adresses non consignées). `userFunding` n'a servi qu'à cette vérification ; le code n'utilisait
alors que `meta`, `candleSnapshot` et `clearinghouseState`.

**Vérifications en direct, lot 3** (2026-10-08, même méthode). `metaAndAssetCtxs` (`dex="xyz"`) pour
le format (`[meta, contextes]` alignés, `funding` horaire) et les taux : `xyz:BRENTOIL` −0,0310 %/h
(un short paie +10,42 % du notionnel en 14 j), `xyz:CL` −0,0113 %/h (+3,80 %). Un cache des 6 perps
× 6 intervalles (pris à 13:07 UTC, un jeudi) a servi, **hors ligne** et horloge figée, aux comparaisons
avant/après de Q12 et à la mesure du veto « chasing » après Q6. L'opérateur a autorisé
`metaAndAssetCtxs` : le code utilise désormais `meta`, `metaAndAssetCtxs`, `candleSnapshot` et
`clearinghouseState`, tous publics, sans clé, en lecture seule.

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
| T4 | Marché sans tendance : EMA plate ⇒ pas de système de tendance | p.77 (PDF 93), règle 3 | `triple_screen.py:92-113` ; `strategy/params.py:18` (`flat_trend_slope_pct = 0.001`) | conforme (principe) · **à trancher** (calibrage) | Seuil en %/barre, non invariant selon le timeframe. Mesuré en direct : tide 1h « neutre » 89 % (GOLD), 99 % (SP500), 62 % (CL) du temps ; tide 4h 63 / 89 / 33 %. L'horizon micro est quasi muet. → **Q1, appliqué** (`fb21597`) : neutre 6–22 % (4h) et 8–31 % (1h). |
| T5 | 2e écran : FI(2) < 0 en tendance haussière ⇒ achat ; > 0 en baissière ⇒ vente ; sinon rester dehors | p.158 (PDF 174) ; tableau p.160 (PDF 176) ; p.113–115 (PDF 129–131) | `triple_screen.py:635-658` | conforme | — |
| T6 | Réserve : pas de signal si FI(2) fait un nouveau plus bas (haut) multi-semaines | p.158 (PDF 174) « as long as it doesn't fall to a new multi-week low » ; p.115 (PDF 131) « lowest low in a month » | `triple_screen.py:137-157`, `:638-656` ; `params.py:23` (25 barres) | conforme | 25 barres 1d (24/7) ≈ 3,5 semaines. |
| T7 | Censure Impulse : rouge interdit l'achat, vert la vente, sur l'hebdo **ou** le quotidien (écrans 1 et 2) ; le 3e écran ne censure jamais | p.163 (PDF 179) ; p.157 (PDF 173) ; p.164 (PDF 180) | `triple_screen.py:660-667` ; tests `test_third_screen_impulse_never_vetoes_*` | conforme | — |
| T8 | Acheter dans/sous la zone de valeur, pas au-dessus (veto directionnel) | p.79–80 (PDF 95–96) ; Trade Apgar p.240 (PDF 256) : au-dessus de la valeur 0, dans la zone 1, sous la valeur 2 | `triple_screen.py:160-194`, `:668-683` ; `params.py:35` (3 %) | conforme (principe directionnel) · **à trancher** (calibrage) | Tolérance fixe de 3 % : mesurée en direct, jamais dépassée sur 1h / 15m (0 %, 0 %, 0,6 % des barres) ⇒ filtre inactif sur scalp/micro ; actif sur swing (GOLD 14 %, CL 57 % des barres 1d). → **Q2 + Q13, appliqués** (`f89a1a6`) : veto par la ligne du canal du wave (p.168). |
| T9 | Entrée en breakout : buy-stop 1 tick au-dessus du plus haut de la barre précédente / sell-stop sous le plus bas ; peut se prendre sur un timeframe plus court | p.158 (PDF 174), p.161 (PDF 177) | `triple_screen.py:332-338`, `:354-360`, `:500-519` | conforme | Tick : voir H10. |
| T10 | Ordre limite à « EMA de demain − pénétration moyenne » sur 4–6 semaines ; EMA projetée = EMA + (EMA − EMA veille) | p.159–160 (PDF 175–176), Fig. 39.3 | `triple_screen.py:116-134`, `:291-296`, `:341`, `:363` ; `params.py:19` (35 barres) | conforme (projection, lookback) · **à trancher** (mesure) | Le code moyenne la pénétration de **chaque barre** sous l'EMA ; Elder mesure **une pénétration par repli** (4 occasions A–D, Fig. 39.3). Les barres d'entrée/sortie d'un repli diluent la moyenne ⇒ limite trop proche. → **Q3, appliqué** (`44616b2`) : une valeur par repli (sa barre la plus profonde), puis la moyenne. |
| T11 | Si non exécuté, abaisser le buy-stop chaque jour au plus haut de la dernière barre + 1 tick, jusqu'à exécution ou retournement de l'indicateur hebdo | p.161 (PDF 177) ; p.115 (PDF 131) | `triple_screen.py:271-288` ; `params.py:41` (`entry_order_expire_bars = 2`) | **à trancher** | Expiration après 2 barres absente du livre. (Le « roll » au plus haut de la dernière barre + 1 tick ne peut pas monter un ordre non exécuté : s'il n'a pas été touché, ce plus haut est resté dessous — conforme à « lower ».) → **Q4, appliqué** (`66aeca6`) : expiration supprimée ; l'ordre vit tant que le tide et la censure Impulse tiennent. |
| T12 | Objectif sur le tide (zone de valeur hebdo), stop sur le wave ; au-delà de la valeur, objectif au canal | p.162 (PDF 178) ; p.171 (PDF 187) ; p.218 (PDF 234) | `triple_screen.py:345-350`, `:367-370` | conforme | — |
| T13 | Récompense/risque ≥ 2:1 | p.154 (PDF 170) ; p.216 (PDF 232) | `params.py:27` ; `triple_screen.py:731` ; `app/pipeline.py:356-371` | conforme | « Signaler sans masquer » est un choix d'outil, cohérent avec p.131 (PDF 147 : objectifs plus petits en day-trading). |
| T14 | Choisir le meilleur trade | Trade Apgar p.238–242 (PDF 254–258) : 5 questions notées 0/1/2, seuil ≥ 7 et aucun zéro ; pour un achat, Impulse hebdo/quotidien **bleu après rouge = 2**, vert = 1, rouge = 0 ; p.164 (PDF 180) : « the best trading signals are given not by green or red but by the loss of green or red » | `triple_screen.py:537-598` (0,40 R:R · 0,25 Impulse **vert** · 0,20 pente tide · 0,15 profondeur FI) | **à trancher** | Le score n'est pas celui d'Elder et récompense l'inverse de l'Apgar sur l'Impulse ; la docstring « Elder's "which setup?" criteria » est inexacte. N'affecte que le classement, jamais une action. → **Q5, appliqué** (`8298f7e`) : Trade Apgar « pullback to value » à 5 lignes, choix = meilleur A-trade. |

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
| I7c | Fenêtre de 100 barres ; un coefficient symétrique | p.79 (PDF 95), p.167 (PDF 183) | `params.py:24` (26 barres) ; `triple_screen.py:323-328` (deux demi-largeurs) | **à trancher** | 26 barres ≠ 100 ; l'asymétrie est un choix documenté du code. → **Q6, appliqué** (`c0b17fe`) : coefficient symétrique unique, 100 barres. |
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
| R3 | La limite de 2 % se calcule sur l'équité du 1er du mois | p.204 (PDF 220) : « Measure your account equity on the first day of each month » | `app/pipeline.py:363-370` (`cfg.risk.equity`) | **à trancher** | Le dimensionnement utilise l'équité courante. → **Q7, appliqué** (`6a25870`) : taille sur `equity_at_month_start`. |
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
| X6 | Ne bouger le stop que dans le sens du trade | p.224 (PDF 240) | `trade_management.py:151-174` (calcul sans mémoire) | **à trancher** | D'un rafraîchissement à l'autre, la suggestion peut baisser (long) : Elder l'interdit. → **Q8, appliqué** (`194ce7d`) : le stop suggéré ne recule jamais (mémoire par position dans le snapshot). |

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
| H5 | Intervalles 5m, 15m, 1h, 4h, 1d, 1w | Tous répondent pour les 6 coins | `hyperliquid.py:28-42` ; `config.py:143-148` | conforme | Note : les bougies **1w commencent le jeudi 00:00 UTC** (alignement epoch), donc la barre hebdo va de jeudi à mercredi. À documenter (→ Q12). **Q12, appliqué** (`a9d4001`) : tide hebdo reconstruit du lundi au vendredi pour le dex `xyz`. |
| H6 | Champs string → float | `o/h/l/c/v` sont des chaînes ; `T − t` = intervalle − 1 ms | `hyperliquid.py:57-84`, `:250-255` | conforme | — |
| H7 | Cache parquet | `:` remplacé dans le nom de fichier | `hyperliquid.py:204-247` | conforme | — |
| H8 | Historique suffisant par horizon | 1w : GOLD 43, SILVER 42, CL 41, BRENTOIL 33, SP500 31, XYZ100 53 barres (< 60) ; 1d : 205–361 ; 4h : 1224–2160 ; 1h, 15m, 5m : ≥ 4895 | `config.toml` (`min_tide_bars = 60`) ; `triple_screen.py:480-485` | conforme | L'avertissement « historique court » se déclenche sur **swing pour les 6 actifs** (vérifié via le pipeline live) ; scalp (1000 barres 4h) et micro (2000 barres 1h) ont assez d'historique. |
| H9 | Avertissement « marché quasi gelé » (week-end) | GOLD 1h : volume médian sam./dim. ≈ 35 contre ≈ 360 en semaine (≈ 10 %), amplitude 0,02–0,03 % contre 0,3 % | `triple_screen.py:487-496` | **erreur** | La référence est la médiane des **60** dernières barres du wave (le paramètre des divergences, réutilisé) : après ~30 barres de week-end, la référence **est** le volume du week-end et l'alerte s'éteint. Rejeu sur 21 j × 6 actifs : alerte sur **196/3456** barres 15m de week-end (micro) et **298/864** barres 1h (scalp). Avec une référence de 1000 barres : 1455/3456 et 397/864, et 4× moins d'alertes en semaine en 15m (le reste correspond au samedi matin, quand la fenêtre de 6 barres contient encore le vendredi). → **C4** |
| H10 | Tick : ≤ 5 chiffres significatifs, ≤ 6 − szDecimals décimales, **entiers toujours admis** (règle Hyperliquid pour les perps) | Pour les 6 coins, la contrainte des 5 chiffres significatifs est active (vérifié sur les chaînes brutes) : GOLD 0,1 ; SILVER 0,001 ; CL 0,001 ; BRENTOIL 0,01 (0,001 sous 100) ; SP500 0,1 ; XYZ100 1 | `triple_screen.py:41-42`, `:85-89` | conforme pour la watchlist par défaut · **erreur latente** | Prix ≥ 100 000 ⇒ tick 10 au lieu de 1 (ex. BTC) ; le plafond 6 − szDecimals est ignoré (actifs bon marché à szDecimals élevé, atteignables via `"*"` / `"xyz:*"`). → **C6** |
| H11 | Arrondi de l'entrée / du stop / de la limite / de l'objectif au tick | Les entrées breakout tombent sur la grille ; stop, limite et objectif ne sont pas arrondis | `triple_screen.py:332-371` | **à trancher** | → **Q9, appliqué** (`5541621`) : tous les niveaux sur la grille, du côté prudent. |
| H12 | Taille arrondie à `szDecimals` | szDecimals : GOLD 4, SILVER 2, CL 3, BRENTOIL 2, SP500 3, XYZ100 4 | `sizing.py:51-52` ; `pipeline.py:363-370` | conforme | Pas de contrôle du notionnel minimum d'un ordre Hyperliquid (10 USD). → **Q10, appliqué** (`2f48417`) : signalé, jamais plafonné. |
| H13 | Levier max | maxLeverage : GOLD 25, SILVER 25, CL 20, BRENTOIL 20, SP500 50, XYZ100 30 ; certains perps xyz sont `onlyIsolated` | non utilisé | **à trancher** | L'Iron Triangle peut proposer un notionnel > maxLeverage × équité (stops serrés sur micro). Aujourd'hui, le seul setup dimensionné (SP500 swing) demande 0,9× l'équité. → **Q10, appliqué** (`2f48417`) : signalé, jamais plafonné. |
| H14 | Coûts de funding | `clearinghouseState` contient `cumFunding` par position (endpoint autorisé) ; les taux courants exigent `metaAndAssetCtxs` (hors des 3 endpoints autorisés par `CLAUDE.md`) | non traité | **à trancher** | → **Q11 phase 1, appliquée** (`115dd04`) : `cumFunding.sinceOpen` affiché. **Phase 2, appliquée** (`6911a1f`) : taux courants via `metaAndAssetCtxs`, autorisé par l'opérateur. |
| H15 | Positions lues par dex | `clearinghouseState` `dex=xyz` renvoie `xyz:GOLD`, `xyz:JPY` (préfixés, `szi` en chaîne) ; l'appel natif ne les contient pas | `pipeline.py:178-209` ; `trade_management.py:102-130` | conforme | — |
| H16 | Un échec sur un dex ne fait pas perdre les autres | Docstring : « a failure on one dex doesn't drop the others » | `pipeline.py:200-206` (ne capture que `HyperliquidError`) | **erreur** | Une erreur HTTP ou un timeout `httpx` sur un dex fait échouer **tout** le rafraîchissement (`build_snapshot` ne capture que `HyperliquidError`). → **C7** |
| H17 | Barres gelées du week-end dans des séries 24/7 | Les perps tradfi impriment des barres le week-end (volume et amplitude ≈ 10 %) | — | **à trancher** | EMA13 en 1d ≈ 9 séances + 4 barres gelées ; Elder raisonne en séances (p.125 : 5 jours/semaine). → **Q12, appliqué** (`a9d4001`) : barres entièrement comprises dans la fermeture du week-end retirées avant tout indicateur. |

---

## 3. Corrections effectuées (erreurs uniquement)

Pour chaque correction, le test a été écrit d'abord et vérifié en **échec** sur le code d'origine
(message d'échec ci-dessous), puis la correction minimale l'a fait passer, avec la suite complète,
ruff et black au vert à chaque commit. Le pipeline complet a ensuite été relancé en direct sur le code
corrigé, sans erreur.

| Id | Commit | Écart | Correction | Test (échec observé avant → passe après) |
|---|---|---|---|---|
| C1 | `9db99dc` | D1 — croisement de la ligne zéro | L'indicateur doit être du bon côté de zéro au premier extrême (négatif pour un creux, positif pour un sommet), pour que le croisement ait lieu entre les deux | `test_divergence_needs_a_real_zero_line_cross_not_an_endpoint` (`['bullish TEST divergence'] == []`) |
| C2 | `f9d08ac` | R5 — risque ouvert au-delà du point mort | Risque = max(0, entrée − stop) × taille pour un long ; max(0, stop − entrée) × taille pour un short | `test_open_risk_is_zero_once_the_stop_locks_in_profit` (`50.0 == 0.0`) |
| C3 | `363406e` | I7b — contenance du canal | Le budget hors canal est réparti sur les deux bords : quantile 1 − (1 − c)/2 par côté | `test_channel_contains_about_95_percent_of_bars` (`0.9 <= 0.846`) |
| C4 | `f56a41e` | H9 — avertissement week-end | Référence de volume dédiée, `low_volume_baseline_bars = 1000` barres du wave, découplée de `divergence_lookback` (ajoutée à `config.toml`) | `test_data_warning_survives_a_whole_frozen_weekend` (aucun avertissement) |
| C5 | `3262ef5` | H2 — coin explicite delisted | `validate_watchlist` refuse un perp `isDelisted` comme un coin inconnu | `test_validate_watchlist_rejects_explicit_delisted_coin` (`DID NOT RAISE`) |
| C6 | `93ec353` | H10 — tick Hyperliquid | Entiers toujours admis (tick ≤ 1) ; plafond 6 − szDecimals ; `szDecimals` transmis par le pipeline aux prix des ordres stop | `test_tick_size_follows_hyperliquid_price_rules` (`10.0 == 1.0`), `test_entry_tick_respects_sz_decimals_cap` (`TypeError`), `test_pipeline_passes_sz_decimals_to_the_strategy` (`{'BTC': None}`) |
| C7 | `c9c629f` | H16 — isolation des échecs par dex | `fetch_open_positions` ignore aussi `httpx.HTTPError` pour le dex fautif | `test_positions_survive_a_failing_dex` (`HTTPStatusError: 502`) |
| C8 | `7e519da` | DOC1 — pages citées | Pages imprimées partout (p.87, p.88, p.115, p.167) ; convention ajoutée à `CLAUDE.md` | — (commentaires et docs seulement, aucun test possible) |

Un test existant encodait l'erreur C2 (`open_risk == abs(entry − stop) × size`) : il a été aligné sur
la formule d'Elder.

---

## 3 bis. Lot 2 — points tranchés appliqués

Même protocole qu'au §3 : test écrit d'abord et vu en **échec** sur le code d'avant (message
ci-dessous), correction minimale, un commit par point, suite complète + ruff + black au vert. Le
dashboard a été rendu dans Chromium sur un snapshot de fixtures (en-tête, ⚠ de taille, colonne de
funding) sans erreur JavaScript.

| Id | Commit | Changement | Test (échec observé avant → passe après) |
|---|---|---|---|
| Q2 + Q13 | `f89a1a6` | Veto « chasing » = canal du wave (p.168) au lieu de 3 % au-delà de la zone de valeur ; `value_zone_max_distance_pct` supprimé ; `value_zone_status` affiché seulement (« extended » = au-delà du canal du wave) | `test_channel_veto_rejects_a_long_above_the_upper_wave_channel` (`'long' == 'stand_aside'`), `…_short_below_the_lower_wave_channel` (`'short' == 'stand_aside'`), `test_a_stretch_beyond_value_inside_the_wave_channel_is_not_vetoed` (`'stand_aside' == 'long'` / `'short'`), `test_value_zone_status_reads_the_wave_channel` (`'extended' == 'near_value'`) |
| Q3 | `44616b2` | Pénétration moyenne = moyenne des maxima de chaque repli contigu (Fig. 39.3) | `test_average_penetration_counts_one_value_per_pullback` (`3.2 == 5.0`), `test_average_penetration_and_projection` (`2.0 == 2.5`) |
| Q4 | `66aeca6` | `entry_order_expire_bars` supprimé ; ordre valide tant que le tide et la censure Impulse tiennent (p.161) | `test_entry_order_plan_rolls_until_the_tide_or_impulse_cancels_it` (le plan disait « roll it… expire after 2 completed 1d bars ») |
| Q7 | `6a25870` | Règle des 2 % sur `equity_at_month_start` (p.204) ; base affichée dans les en-têtes | `test_two_percent_rule_sizes_on_equity_at_month_start` (`120.0 == 80.0`) |
| Q9 | `5541621` | `round_to_tick()` ; entrée, limite, stop, objectif et stop de la limite arrondis du côté prudent ; R:R et taille sur les niveaux arrondis | `test_round_to_tick_on_the_requested_side` (`ImportError`), `test_levels_round_to_the_tick_on_the_prudent_side` (niveau hors grille : `1425.2485 == 1425` en ticks), `test_size_is_worked_out_from_the_rounded_entry_and_stop` (idem) |
| Q10 | `2f48417` | `PerpSpec` (szDecimals, maxLeverage, onlyIsolated) renvoyé par `validate_watchlist` / `tradable_perps` ; `size_warnings` (levier > max, < 10 USD) au dashboard et au CLI | `test_meta_exposes_leverage_limits` (`ImportError: PerpSpec`), `test_size_warnings_flag_sizes_the_exchange_would_refuse` (`ImportError: size_warnings`), `test_unexecutable_size_is_flagged_never_capped` (ligne `! size:` absente du CLI) |
| Q11 (phase 1) | `115dd04` | `cumFunding.sinceOpen` lu par `parse_positions` (`cum_funding`), affiché au dashboard, au CLI et dans le journal | `test_parse_positions_reads_funding_paid_since_open` (`AttributeError: … 'cum_funding'`), `test_funding_paid_reaches_the_snapshot_cli_and_journal` (`KeyError: 'cum_funding'`) |

Tests existants alignés : les deux tests du veto « extended » passaient `value_zone_max_distance_pct=0.0`
(remplacés par les tests du canal ; le test directionnel « long sous la valeur » est conservé sans ce
paramètre) ; deux tests encodaient la moyenne par barre (`2.0` → `2.5`) ; le test du plan d'ordre
exigeait « expire after » ; les tests de `validate_watchlist` / `tradable_perps` comparent désormais
les `szDecimals` des `PerpSpec`. `synthetic_candles()` accepte une série `closes` pour rejouer un setup
long dans le pipeline.

---

## 3 ter. Lot 3 — points tranchés appliqués

Même protocole (test vu en **échec** d'abord, un commit par point, suite + ruff + black au vert).
Décisions de l'opérateur appliquées telles quelles ; écarts au livre signalés.

| Id | Commit | Changement | Test (échec observé avant → passe après) |
|---|---|---|---|
| Q11 (phase 2) | `6911a1f`, `46085fe` (affichage des taux minuscules) | `metaAndAssetCtxs` ajouté à la liste des endpoints autorisés (`CLAUDE.md`, docstring du client) ; `funding_rates(dex)` ; clé `holding_hours` par horizon (swing 336, scalp 48, micro 4) ; par signal : taux horaire, coût estimé en % du notionnel signé selon le sens (+ = payé), `funding_warning` quand il dépasse la moitié du risque (\|entrée − stop\| / entrée) ; dashboard + CLI ; l'action ne change jamais | `test_funding_rates_come_from_meta_and_asset_ctxs`, `test_funding_rates_reject_a_misaligned_payload` (`AttributeError: … 'funding_rates'`), `test_funding_cost_is_signed_by_the_side_of_the_trade`, `test_funding_warning_when_the_cost_exceeds_half_the_trade_risk`, `test_signals_carry_the_funding_rate_and_its_cost`, `test_a_failing_funding_lookup_drops_only_that_dex` (`ImportError: fetch_funding_rates`), tests de config (`AttributeError: … 'holding_hours'`) |
| Q5 | `8298f7e` | `trade_apgar()` : tide Impulse vert 2 / bleu 1 / rouge 0 ; wave Impulse bleu 2 / vert 1 / rouge 0 ; clôture du wave sous la valeur 2 / dedans 1 / au-dessus 0 ; R:R ≥ 2 → 2, ≥ 1 → 1, sinon 0 ; divergence du wave favorable 2 / aucune 1 / défavorable 0 (miroir pour un short). A-trade = ≥ 7 et aucun 0 ; `select_best` = meilleur Apgar parmi les A-trades, départage par R:R. Détail des 5 lignes au dashboard et au CLI ; le journal garde total + A-trade. Supprimés : `compute_quality_score`, `impulse_confirmation`, `pullback_quality`, `tide_slope_strength`/`tide_strength` (ne servaient qu'au score), `score_*`, `rr_excellent`, `strong_tide_slope`, `fi_scale_lookback` | `test_trade_apgar_*`, `test_an_a_trade_needs_seven_points_and_no_zero_line`, `test_select_best_*`, `test_signal_carries_its_trade_apgar` (`ImportError: TradeApgar`), `test_trade_apgar_reaches_the_snapshot_and_the_cli` (`KeyError: 'apgar'`) |
| Q6 | `c0b17fe` | `channel()` : plus petit k tel qu'au moins `channel_containment` des barres de la fenêtre aient high ≤ EMA26·(1+k) et low ≥ EMA26·(1−k), chaque barre contre sa propre EMA ; lignes EMA26·(1 ± k) ; `channel_lookback_bars = 100` (tout l'historique s'il est plus court) | `test_channel_is_one_symmetric_coefficient_around_the_slow_ema` (`4.7 == 0.5` : demi-largeurs inégales), `test_channel_fits_the_last_100_bars_or_all_there_is` (`26 == 100`) |
| Q8 | `194ce7d` | `stop_memory` dans le snapshot (clé actif\|sens\|entrée) ; `assess_position(previous_stop=…)` : max (long) / min (short), donc raison affichée et risque ouvert (règle des 6 %) sur le stop retenu ; nouvelle clé = remise à zéro ; `build_snapshot(previous=…)` et `refresh_horizon` relisent le snapshot, `run.py` le charge aussi pour un rafraîchissement complet | `test_suggested_stop_never_moves_against_the_trade` (`TypeError: … 'previous_stop'`), `test_a_suggested_stop_never_moves_back_across_refreshes`, `test_stop_memory_resets_when_the_position_changes`, `test_cli_refresh_reads_the_previous_snapshot_on_both_paths` (`ImportError: stop_memory_key`) |
| Q12 | `a9d4001` | `data/sessions.py` + `[sessions.xyz]` (ven. 21:00 → dim. 22:00 UTC) : barres entièrement comprises dans la fermeture retirées avant tout indicateur (tous les écrans, positions comprises) ; écran 1w reconstruit depuis les barres 1d du lundi au vendredi ; jours fériés laissés à l'avertissement « marché quasi gelé » | `tests/test_sessions.py`, `test_parses_a_weekend_closure_per_dex`, `test_xyz_bars_skip_the_weekend_and_the_weekly_tide_is_built_from_weekdays` (`ModuleNotFoundError: data.sessions`) |

Tests existants alignés : `test_quality_score_rewards_better_reward_risk` et
`test_impulse_confirmation_counts_agreeing_screens` supprimés avec leurs fonctions ; les tests de
`select_best` réécrits pour l'Apgar ; le choix du snapshot de fixtures doit être un A-trade (et non plus
`rr_ok`) ; `test_channel_backbone_is_slow_ema26` vérifie que le canal est **centré** sur l'EMA26 (la
ligne basse d'un canal symétrique ne se pose plus sur elle) ; la config de test déclare `holding_hours`.
`test_channel_contains_about_95_percent_of_bars` et `test_channel_lower_band_stays_positive_after_a_crash`
sont conservés tels quels.

**Écart assumé au livre (Q5).** La ligne « tide Impulse » de la Fig. 58.1 donne 2 au bleu-après-rouge
et 1 au vert ; ce système, qui achète un repli *avec* la marée, donne 2 au vert et 1 au bleu. Elder
l'autorise (p.242 : « each strategy demands its own Apgar »). Le choix ne requiert plus `rr_ok` : un
A-trade à 1,5:1 (9 points) est éligible ; il reste signalé ⚠ sous 2:1.

**Effet de Q6 sur le veto « chasing » (Q2).** Même cache, barres filtrées par Q12, part des clôtures du
wave au-delà du canal, par côté (au-dessus / au-dessous), canal du lot 2 → canal Q6 : 1h 1,8–3,7 % →
1,4–2,2 % ; 15m 2,3–3,4 % → 0,8–2,1 % ; 1d 0–5,7 % → 0 % pour 4 actifs sur 6 (GOLD 1,3 %, XYZ100
5,3 %/1,4 %). Sur 1d, un seul coefficient doit couvrir le côté le plus agité (k médian ≈ 10–17 % pour
l'or, l'argent et le pétrole sur 187–249 barres) : le veto ne joue presque plus sur swing.

**Écart mesuré par Q12** (même code et mêmes barres, avec et sans calendrier, horloge figée) :
- barres retirées : samedi en 1d (−14 %), ≈ 26 % des barres 4h, ≈ 29 % des barres 1h / 15m / 5m ;
  tide swing reconstruit du lundi au vendredi (29–51 barres hebdo, contre 30–52 bougies 1w jeudi→mercredi) ;
- à 13:07 UTC (jeudi) : 9 signaux sur 18 changent un niveau, une Impulse ou le R:R (GOLD swing :
  Impulse du wave rouge → bleu ; SP500 swing : Impulse du tide vert → bleu ; CL micro : R:R
  0,36 → 1,05), **aucune action ne change** ;
- rejeu de chaque clôture de barre du wave (swing, scalp : 200 dernières barres ; micro : 800) :
  l'action diffère sur 9 % / 3 % / 1 % des décisions de semaine (swing / scalp / micro) et le tide sur
  20 % / 9 % / 2 % ; aux instants de week-end, l'action diffère sur 7 % / 38 % / 49 % — avec le
  calendrier, un instant de week-end reprend le signal de la dernière barre de séance jusqu'à la
  réouverture.

---

## 4. Points à trancher — recommandations et décisions (toutes appliquées depuis le lot 3)

| Id | Sujet | Constat | Recommandation |
|---|---|---|---|
| Q1 | `flat_trend_slope_pct` par horizon (T4) | Médiane de \|pente EMA13\| par barre : 1w 0,25–0,62 % ; 1d 0,12–0,58 % ; 4h 0,04–0,18 % ; 1h 0,015–0,07 %. Avec 0,1 %, le tide 1h est « neutre » 89–99 % du temps (GOLD/SP500). | **Appliqué (lot 1, `fb21597`).** Le bruit croît comme √temps : chaque horizon prend la valeur swing × √(barre du tide / 1 semaine) — scalp `0.00015`, micro `0.00008`, swing inchangé (`0.001`), via `[scanner.horizons.strategy]`. Part neutre sur les 6 perps : 6–22 % (4h), 8–31 % (1h), contre 0–20 % sur swing. Un test verrouille la règle sur le `config.toml` livré. |
| Q2 | `value_zone_max_distance_pct` par horizon (T8) | 3 % n'est jamais atteint sur 1h/15m ⇒ le veto « chasing » ne sert que sur swing. | **Appliqué (lot 2, `f89a1a6`), avec Q13.** Long refusé si la clôture du wave est au-dessus de la ligne haute du canal du **wave**, short si elle est sous la ligne basse (`channel()` réutilisé sur le frame wave). `value_zone_max_distance_pct` supprimé (`params.py`, `config.toml`) ; `value_zone_status` reste affiché (« extended » = au-delà du canal du wave), sans veto. Mesure en direct (mission) : avec 3 %, le veto ne touche jamais les barres 1h/15m et touche 0,7 à 31,5 % des barres 1d selon l'actif ; avec le canal, 2 à 4 % partout. Recompte du 2026-10-08, par côté (clôture au-dessus / au-dessous), ≤ 5000 barres : 3 % → 0 % (15m), 0–1,2 % (1h), 0–28,8 % (1d) ; canal → 1,7–3,4 % (1h, 15m), 0,8–5,1 % (1d, 204–360 barres seulement). |
| Q3 | Pénétration moyenne par barre ou par repli (T10) | Elder : une valeur par repli (Fig. 39.3) ; code : chaque barre. | **Appliqué (lot 2, `44616b2`).** Chaque série contiguë de barres qui percent l'EMA13 (au-dessus pour `side="up"`) est un repli, mesuré à sa barre la plus profonde ; la limite utilise la moyenne de ces maxima. Mesure en direct (mission) : limite 1,1 à 2,8 fois plus profonde (GOLD 1d : 2,0 % → 3,5 % sous l'EMA projetée). Recompte (côté achat, 6 perps × 1d/1h/15m) : ×1,07 à ×2,84 ; GOLD 1d 1,99 % → 3,49 %. |
| Q4 | Cycle de vie de l'ordre stop (T11) | Expiration après 2 barres : absente du livre (le « roll » est, lui, conforme). | **Appliqué (lot 2, `66aeca6`).** `entry_order_expire_bars` supprimé (`params.py`, `config.toml`). Le plan d'ordre : buy-stop abaissé (sell-stop relevé) à chaque barre du wave au plus haut + 1 tick (plus bas − 1 tick), valide jusqu'à exécution tant que le tide tient et qu'aucune Impulse ne censure le trade (p.161 : « until stopped in or until the weekly indicator reverses »). |
| Q5 | Score de classement vs Trade Apgar (T14) | Pondération maison, Impulse vert récompensé ; l'Apgar récompense le bleu après rouge. | Remplacer par un Apgar « pullback to value » (Impulse tide, Impulse wave, prix vs valeur, R:R, profondeur du repli ; 0/1/2 chacun, ≥ 7 et aucun zéro pour être « A-trade ») ; **Appliqué (lot 3, `8298f7e`)**, barème de l'opérateur : tide Impulse vert 2 / bleu 1 / rouge 0 ; wave Impulse bleu 2 / vert 1 / rouge 0 ; clôture du wave sous la valeur 2 / dedans 1 / au-dessus 0 ; R:R ≥ 2 → 2, 1–2 → 1, < 1 → 0 ; divergence du wave favorable 2 / aucune 1 / défavorable 0 (miroir pour un short). A-trade = ≥ 7 sans zéro ; choix = meilleur Apgar parmi les A-trades, départage par R:R, aucun sinon. Écart à la Fig. 58.1 sur la ligne hebdo, voir §3 ter. |
| Q6 | Canal : 100 barres et coefficient symétrique (I7c) | Avec 100 barres + quantile par côté corrigé : 90,5–95,1 % (1w), 94 % (4h/1h). Swing n'a que 31–53 barres hebdo. | `channel_lookback_bars = 100` (valeur du livre ; utilise tout l'historique disponible si plus court) ; **Appliqué (lot 3, `c0b17fe`)** : coefficient symétrique unique (p.167), plus petit k contenant ≥ 95 % des 100 dernières barres (p.79), chaque barre contre son EMA26 ; tout l'historique s'il est plus court. Effet sur le veto « chasing » : voir §3 ter (quasi nul sur 1d). |
| Q7 | Base de la règle des 2 % (R3) | Elder fige la limite sur l'équité du 1er du mois. | **Appliqué (lot 2, `6a25870`).** `_finalize_block` dimensionne sur `equity_at_month_start` (p.204 : « Measure your account equity on the first day of each month ») ; l'en-tête du dashboard et du CLI indique cette base. |
| Q8 | Stop qui recule (X6) | La suggestion est recalculée à chaque passage, sans mémoire. | Afficher « ne jamais baisser un stop existant » à côté de la suggestion ; **Appliqué (lot 3, `194ce7d`)**, avec un petit état persistant : `stop_memory` du snapshot (clé actif, sens, entrée) ; suggestion = max(ancien, nouveau) pour un long, min pour un short, remise à zéro si la clé change ; `build_snapshot` et `refresh_horizon` relisent le snapshot précédent. L'outil ignore toujours le stop réellement posé (ordres non lus). |
| Q9 | Arrondi des niveaux au tick (H11) | Stop, limite et objectif non arrondis. | **Appliqué (lot 2, `5541621`).** `round_to_tick(price, direction, sz_decimals)` sur `tick_size()` : buy-stop ↑, sell-stop ↓, stop long ↓, stop short ↑, limite d'achat ↓, limite de vente ↑, objectif vers l'entrée ; le stop de la limite suit le stop. R:R et taille (Iron Triangle) sont calculés sur l'entrée et le stop arrondis. Hors périmètre : le trailing stop des positions ouvertes (`trade_management`) n'est pas arrondi. |
| Q10 | Levier max et notionnel minimum (H12, H13) | Non contrôlés. | **Appliqué (lot 2, `2f48417`).** `validate_watchlist` et `tradable_perps` renvoient un `PerpSpec` (`sz_decimals`, `max_leverage`, `only_isolated`) ; pipeline et Protocol suivent. Avertissement `size_warnings` quand taille × entrée > maxLeverage × équité courante (marge isolée mentionnée) ou < 10 USD, affiché au dashboard (⚠ sur la taille + raison) et au CLI (`! size:`) ; ni l'action ni la taille ne changent. Exemple en direct (mission) : un short SP500 sur micro demandait 10,4 × l'équité (max 50×). |
| Q11 | Funding (H14) | Non traité. | **Phase 1 appliquée (lot 2, `115dd04`).** `parse_positions` lit `cumFunding.sinceOpen` (`cum_funding`), affiché comme « funding payé » au dashboard, au CLI et dans le journal. Signe vérifié en direct sur 6 positions `xyz` publiques : opposé à la somme des `userFunding.usdc` (négatif = payé) ⇒ positif = payé, négatif = reçu. **Phase 2 appliquée (lot 3, `6911a1f`)** : l'opérateur autorise `metaAndAssetCtxs` (liste de `CLAUDE.md` amendée) ; taux horaire (positif = les longs paient) et coût estimé sur `holding_hours` (swing 14 j, scalp 2 j, micro 4 h), en % du notionnel signé selon le sens ; avertissement au-delà de la moitié du risque du trade, sans effet sur l'action. Mesure en direct : BRENTOIL −0,031 %/h ⇒ ≈ 10,4 % pour un short de 14 j ; CL ≈ 3,8 %. |
| Q12 | Séances vs 24/7 pour les perps tradfi (H5, H17) | Barres gelées du week-end dans les EMA ; barres hebdo jeudi→mercredi. | Au minimum, documenter les deux faits dans le README ; **Appliqué (lot 3, `a9d4001`)**, option lourde : filtre calendaire par dex (`[sessions.xyz]` : ven. 21:00 → dim. 22:00 UTC) avant tout indicateur, tide hebdo reconstruit du lundi au vendredi ; jours fériés laissés à l'avertissement « marché quasi gelé ». Écart avant/après mesuré au §3 ter, documenté dans le README. |
| Q13 | Règle d'Elder « jamais acheter au-dessus du canal supérieur ni vendre sous l'inférieur » | p.168 (PDF 184). Non implémentée ; le veto de la zone de valeur la couvre en partie. | **Appliqué avec Q2 (lot 2, `f89a1a6`)** : c'est désormais le veto « chasing », sur le canal du wave. |

---

## 5. Bilan

- **Points vérifiés** : 60 (Triple Screen 14, indicateurs 10, divergences 4, risque 8, sorties 6,
  documentation 1, Hyperliquid 17).
- **Conformes ou écarts justifiés** : 42, dont 3 conformes sur le principe mais à recalibrer (T4, T8,
  T10).
- **Erreurs** : 8, toutes corrigées (C1–C8), dont une latente pour la watchlist par défaut (C6) et une
  purement documentaire (C8).
- **À trancher** : 9 points sans verdict, 1 raffinement facultatif non implémenté (D4) ; au total
  13 questions (Q1–Q13). Q1 est appliquée (lot 1) : scalp et micro produisent de nouveau des setups.
- **Lot 2** : Q2 et Q13 (`f89a1a6`), Q3 (`44616b2`), Q4 (`66aeca6`), Q7 (`6a25870`), Q9 (`5541621`),
  Q10 (`2f48417`) et Q11 phase 1 (`115dd04`) sont appliquées (§3 bis).
- **Lot 3** : Q11 phase 2 (`6911a1f`, `46085fe`), Q5 (`8298f7e`), Q6 (`c0b17fe`), Q8 (`194ce7d`) et Q12
  (`a9d4001`) sont appliquées (§3 ter). Les 13 questions sont tranchées. Restent ouverts : D4
  (raffinement facultatif des divergences), l'arrondi au tick du trailing stop des positions ouvertes
  (hors du périmètre de Q9), et deux effets à surveiller — le veto « chasing » quasi inactif sur 1d
  avec le canal symétrique, et, le week-end, un signal tradfi qui reprend celui de la dernière barre de
  séance sans avertissement dédié.
