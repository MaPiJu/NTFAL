# Déploiement du bot sur un VPS — guide pas à pas

Ce guide met `bot.py` en service sur un VPS Linux. Le bot trade automatiquement **un seul
horizon** (swing par défaut) du Triple Screen et envoie un **rapport quotidien sur
Telegram**, avec une alerte immédiate à chaque ordre d'entrée, exécution, clôture ou erreur.

> ⚠️ C'est de l'argent réel. Le bot applique les règles d'Elder (règle des 2 %, règle des
> 6 %, censure Impulse, stops SafeZone), mais une erreur de configuration, un bug ou un
> marché qui gappe peuvent coûter plus que prévu. Respecte l'ordre : **dry-run →
> (testnet) → mainnet**, avec une petite équité au début.

## 1. Ce que fait le bot, précisément

- **Une fois par barre d'entrée** de l'horizon tradé (swing : toutes les 4 h, 60 s après
  la clôture de la barre), il rafraîchit le Triple Screen puis :
  - **positions qu'il a ouvertes** : verdict Elder `EXIT` → clôture totale (ordre IOC
    limité à `exit_slippage_pct` du mark) ; premier verdict `TAKE_PROFITS` → clôture de
    `take_profit_fraction` (50 %) ; sinon le stop suit le stop SafeZone, **uniquement dans
    le sens du trade** ;
  - **entrée en attente** : il la suit (Elder p.161), c'est-à-dire qu'il déplace le
    buy-stop sur le plus haut de la dernière barre 4h + 1 tick tant que le signal tient. Il
    l'annule dès que le signal tombe (marée retournée, censure Impulse, plus un A-trade…) ;
  - **nouvelle entrée** : s'il n'y a aucune entrée en attente, il choisit le meilleur
    setup qui est un **A-trade** (Apgar ≥ 7 sans zéro) avec un **R:R ≥ 2**. Cet actif ne
    doit être tenu par aucune position, même manuelle. Le bot pose alors un
    **stop-limit** au niveau du stop-entry du signal.
- **Toutes les 60 s** (`protect_seconds`), il vérifie chaque position qu'il a ouverte.
  Chacune doit porter **exactement un stop** (stop-market, reduce-only) sur l'exchange,
  dimensionné à la position et jamais desserré. Une entrée exécutée reçoit donc son stop
  en moins d'une minute.
- **Chaque jour à `report_time`**, il envoie le rapport Telegram : équité, PnL 24 h et du
  mois, règle des 6 %, exécutions, actions du bot et ce qu'il n'a pas fait (avec la
  raison), positions avec leur verdict et leur stop, entrée en attente, signaux swing.

Règles de sécurité intégrées :

| Règle | Effet |
|-------|-------|
| Règle des 2 % | La taille est calculée sur le **pire remplissage** (la limite du stop-limit) : même rempli à la limite, le trade ne risque pas plus que `risk_pct` (1 %) de l'équité de début de mois. |
| Règle des 6 % | Pas d'entrée si les pertes du mois + le risque ouvert + **le risque du nouveau trade** dépassent 6 %. L'entrée en attente est annulée si la règle se déclenche. |
| Équité automatique | Lue sur le compte (`clearinghouseState`). La première lecture du mois fixe l'équité de début de mois. Pertes du mois = PnL net des exécutions du mois (`userFillsByTime`). Si la lecture échoue : aucune entrée. |
| Levier | Choisi par trade pour que la liquidation (marge isolée) tombe au moins ~2 fois plus loin que le stop. Plafonné par `max_leverage` et par le maximum du marché. |
| Week-end xyz | Pas d'entrée pendant la fermeture (ven. 17:00 → dim. 18:00, heure de New York). L'entrée en attente est annulée dès la fermeture. Les sorties attendent la réouverture ; **le stop reste en place**. |
| Données douteuses | Les drapeaux `low_volume` et `market_closed` bloquent une nouvelle entrée (`block_on_flags`). |
| Positions manuelles | Le bot ne touche **jamais** une position qu'il n'a pas ouverte, mais il n'entre pas sur un actif déjà tenu. |
| Pause | Le fichier `cache/HALT` empêche toute nouvelle entrée et annule celle en attente. Les positions gardent leur stop et leurs sorties. |
| Clé | Le bot exige une **API wallet** et refuse la clé du compte principal. |

## 2. Préparer Hyperliquid

1. **Crée une API wallet** : app.hyperliquid.xyz → *More* → *API* → nomme-la (ex.
   `ntfal-vps`), choisis une durée de validité, *Generate*, puis *Authorize* depuis ton
   wallet principal. **Copie la clé privée affichée** : c'est `HL_API_PRIVATE_KEY`. Cette
   clé peut placer et annuler des ordres pour ton compte, mais **ne peut pas retirer de
   fonds**. Note sa date d'expiration : à l'expiration, les ordres sont refusés et le bot
   t'alerte.
2. Ton adresse publique de compte (celle du wallet principal) est `HL_ADDRESS`.
3. **Marchés xyz (HIP-3)** : ils ont leur propre chambre de compensation et leur propre
   marge. Vérifie dans l'interface que ton compte peut trader `xyz:GOLD` avec de l'USDC
   disponible pour ce dex. Sinon les ordres sont refusés (« insufficient margin ») et le
   bot t'alerte.
4. Si l'API wallet est compromise : révoque-la depuis la même page *API*. Elle ne pouvait
   de toute façon pas retirer.

## 3. Créer le bot Telegram

1. Sur Telegram, ouvre **@BotFather** → `/newbot` → choisis un nom. Il te donne le
   **token** (`TELEGRAM_BOT_TOKEN`).
2. Envoie un message quelconque à ton nouveau bot.
3. Ouvre `https://api.telegram.org/bot<TOKEN>/getUpdates` dans un navigateur et relève
   `"chat":{"id": …}` : c'est `TELEGRAM_CHAT_ID`.

## 4. Installer sur le VPS

Exemple sur Ubuntu 24.04, en root :

```sh
adduser --system --group --no-create-home --home /opt/ntfal ntfal
apt install -y git python3 python3-venv
git clone https://github.com/MaPiJu/NTFAL.git /opt/ntfal   # (dépôt privé : clé de déploiement SSH)
cd /opt/ntfal
python3 -m venv .venv
.venv/bin/pip install httpx pandas pyarrow fastapi uvicorn jinja2 python-dotenv apscheduler tzdata \
    "hyperliquid-python-sdk>=0.24,<0.25"
mkdir -p cache
cp .env.example .env
chown -R ntfal:ntfal /opt/ntfal
chmod 600 .env
```

(Avec `uv` : `uv sync --extra bot` crée le même `.venv`.)

Renseigne `.env` (`nano /opt/ntfal/.env`) :

```sh
HL_ADDRESS=0x…ton adresse publique…
HL_API_PRIVATE_KEY=0x…clé de l'API wallet…
TELEGRAM_BOT_TOKEN=…
TELEGRAM_CHAT_ID=…
```

Dans `config.toml` :

```toml
[execution]
enabled = true
mode = "dry_run"      # on commence TOUJOURS ici

[telegram]
enabled = true
report_time = "21:15"
timezone = "Europe/Paris"
```

Vérifie ensuite :

```sh
sudo -u ntfal .venv/bin/python bot.py --report   # le rapport arrive-t-il sur Telegram ?
sudo -u ntfal .venv/bin/python bot.py --once     # un cycle : ce que le bot ferait
```

## 5. Les trois étapes

1. **Dry-run, quelques jours à quelques semaines** (avec `HL_ADDRESS` renseignée : le bot y
   lit l'équité et les positions ; sans adresse, mets `auto_equity = false`, sinon il ne
   place aucune entrée). Le bot calcule, journalise et rapporte
   chaque ordre (« entry placed », « trailed », « cancelled »…) **sans rien envoyer**. Ses
   entrées ne s'exécutent jamais, donc il n'ouvre aucune position. Lis les rapports et
   compare-les au tableau de bord : les niveaux et les tailles doivent être ceux que tu
   aurais pris à la main.
2. **Testnet (facultatif mais recommandé)** : `mode = "testnet"`, avec une API wallet créée
   sur app.hyperliquid-testnet.xyz. Données et ordres viennent du testnet, dans un cache
   séparé (`cache/testnet/`). Les marchés xyz n'y existent peut-être pas : mets
   temporairement `watchlist = ["BTC", "ETH"]`. On vérifie ici la **mécanique** : ordres
   acceptés, stop posé après exécution, stop suiveur, annulation au week-end.
3. **Mainnet** : `mode = "mainnet"`, de préférence avec un compte peu approvisionné au
   début. Au démarrage, la console affiche un bandeau `MAINNET` et Telegram reçoit
   « bot started — mode MAINNET ».

## 6. Mettre en service

```sh
cp /opt/ntfal/deploy/ntfal-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ntfal-bot
journalctl -u ntfal-bot -f                     # suivre le journal
```

| Besoin | Commande |
|--------|----------|
| Pause (plus d'entrée, sorties et stops actifs) | `sudo -u ntfal touch /opt/ntfal/cache/HALT` |
| Reprendre | `rm /opt/ntfal/cache/HALT` |
| Arrêter le bot | `systemctl stop ntfal-bot` (les stops restent sur l'exchange) |
| Mettre à jour | `cd /opt/ntfal && sudo -u ntfal git pull && systemctl restart ntfal-bot` |
| Rapport immédiat | `sudo -u ntfal .venv/bin/python bot.py --report` |

L'état du bot (positions qu'il possède, entrée en attente, journal des actions) est dans
`cache/execution_state.json`. **Ne le supprime pas pendant qu'une position est ouverte.**
Si c'est arrivé quand même, le bot reprend la gestion de toute position qui porte encore
un de ses stops sur l'exchange.

## 7. Sécuriser le VPS

- Connexion SSH par clé uniquement (`PasswordAuthentication no`), `ufw allow OpenSSH &&
  ufw enable`, `apt install unattended-upgrades fail2ban`.
- **N'expose pas le tableau de bord** sur Internet. Si tu le veux, lance plutôt
  `bot.py --serve` (écoute sur 127.0.0.1) et ouvre un tunnel :
  `ssh -L 8000:127.0.0.1:8000 vps`, puis http://127.0.0.1:8000.
- `.env` en `chmod 600`, propriété de `ntfal`. Le token Telegram n'apparaît jamais dans
  les journaux.

## 8. Limites connues — à vérifier sur testnet avant le mainnet

- Le bot gère **un seul horizon**, celui des positions (`positions_horizon`). Les autres
  horizons restent indicatifs.
- Une entrée stop-limit sautée par un gap (prix au-delà de la limite) reste posée comme
  limite. Elle ne s'exécute que si le prix revient dans la limite, donc sans dépasser le
  risque prévu.
- Une sortie IOC qui ne trouve pas de liquidité dans sa limite est retentée au cycle
  suivant. Le stop reste en place entre-temps, et Telegram t'alerte.
- La mécanique d'ordre suit le SDK officiel `hyperliquid-python-sdk` : stop-limit
  d'entrée = déclencheur `tpsl: "sl"` non reduce-only, stop = déclencheur market
  reduce-only. **Vérifie-la sur testnet** : l'API d'Hyperliquid peut évoluer.
- Ce n'est pas un conseil financier.
