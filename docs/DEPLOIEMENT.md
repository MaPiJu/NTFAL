# Déploiement du bot sur un VPS — guide pas à pas

Ce guide met `bot.py` en service sur un VPS Linux, avec **un seul service systemd** qui
fait tout :

- il garde **les trois horizons à jour**, chacun juste après chaque clôture de sa bougie
  d'entrée (swing toutes les 4 h, scalp toutes les 15 min, micro toutes les 5 min) ;
- il sert le **tableau de bord** sur 127.0.0.1:8000, accessible par un tunnel SSH ;
- il envoie le **rapport quotidien sur Telegram** ;
- **une fois l'exécution activée**, il trade automatiquement **un seul horizon** (swing par
  défaut) du Triple Screen, avec une alerte immédiate à chaque ordre d'entrée, exécution,
  clôture ou erreur.

Tant que `[execution] enabled = false`, c'est un outil d'analyse seul : aucun ordre n'est
calculé ni envoyé.

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

Seuls les marchés qui détiennent au moins `min_open_interest_usd` d'open interest (10 M$
par défaut) sont analysés : un carnet mince veut dire des spreads larges et un stop qui
glisse. Les six marchés par défaut en détiennent chacun 150 à 340 M$. Une position déjà
ouverte reste gérée, quel que soit l'open interest de son marché.

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

Le dépôt est cloné dans `/opt/ntfal` (déjà fait). En root, depuis le serveur :

```sh
cd /opt/ntfal
git pull                          # la première fois seulement : récupère install.sh
sudo ./deploy/install.sh --dev    # --dev : ajoute pytest, pour lancer les tests
```

Ce `git pull` ne passe que si tu n'as encore rien modifié dans les fichiers suivis
(`config.toml`). Ensuite, mets toujours à jour avec `sudo ./deploy/install.sh --update` :
il met tes modifications de côté pendant la mise à jour, puis les réapplique. Si elles
entrent en conflit avec la nouvelle version, il s'arrête sans toucher au service et te
dit quoi faire.

Le script est idempotent (on peut le relancer sans risque). Il :

1. installe `git`, `python3` et `python3-venv`, et vérifie que Python est en version 3.11
   ou plus ;
2. crée l'utilisateur système `ntfal`, sans shell ni mot de passe ;
3. crée ou met à jour `.venv` avec les dépendances de `pyproject.toml`, SDK Hyperliquid
   compris ;
4. crée `.env` depuis `.env.example` s'il n'existe pas, sans jamais l'écraser ;
5. règle les droits : le code appartient à root et reste en lecture seule pour le
   service ; `cache/` et `.env` (en `chmod 600`) appartiennent à `ntfal` ;
6. vérifie, en tant que `ntfal`, que `config.toml` se charge et que chaque secret exigé
   par la configuration est bien dans `.env` (il nomme celui qui manque, sans jamais
   afficher de valeur) ;
7. installe le service `ntfal-bot`, l'active au démarrage et le (re)lance.

Juste après l'installation, le service tourne en **analyse seule**. Vérifie-le :

```sh
systemctl status ntfal-bot          # « active (running) »
journalctl -u ntfal-bot -f          # le journal en direct (Ctrl-C pour quitter)
.venv/bin/python -m pytest -q       # les tests (avec --dev)
```

**Ouvrir le tableau de bord** depuis ton ordinateur (le service n'écoute que sur
127.0.0.1, rien n'est exposé sur Internet) :

```sh
ssh -L 8000:127.0.0.1:8000 root@2.31.30.116
```

Laisse cette fenêtre ouverte et va sur http://127.0.0.1:8000.

Renseigne ensuite `.env` (`nano /opt/ntfal/.env`), au fur et à mesure des étapes :

```sh
HL_ADDRESS=0x…ton adresse publique…        # dès le dry-run
TELEGRAM_BOT_TOKEN=…                        # dès que tu actives Telegram
TELEGRAM_CHAT_ID=…
HL_API_PRIVATE_KEY=0x…clé de l'API wallet…  # seulement pour testnet / mainnet
```

Tape ces valeurs directement dans `nano` sur le serveur. Ne les colle jamais dans un
chat, un ticket ou un fichier versionné.

Puis dans `config.toml` :

```toml
[execution]
enabled = true
mode = "dry_run"      # on commence TOUJOURS ici

[telegram]
enabled = true
report_time = "21:15"
timezone = "Europe/Paris"
```

Relance le script pour appliquer et tout revérifier (un secret manquant l'arrête avant
le redémarrage) :

```sh
sudo ./deploy/install.sh
journalctl -u ntfal-bot -n 60                    # le cycle de démarrage : ce que le bot ferait
sudo -u ntfal .venv/bin/python bot.py --report   # le rapport arrive-t-il sur Telegram ?
```

Au démarrage, le service lance tout de suite un cycle de trading et une vérification des
stops : le journal montre ce que le bot ferait (« entry placed », « no setup »…). Évite
`bot.py --once` pendant que le service tourne : deux processus écriraient le même état.

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
   temporairement `watchlist = ["BTC", "ETH"]`. Le plancher d'open interest est désactivé
   en testnet, où l'open interest est de l'argent fictif. On vérifie ici la **mécanique** :
   ordres acceptés, stop posé après exécution, stop suiveur, annulation au week-end.
3. **Mainnet** : `mode = "mainnet"`, de préférence avec un compte peu approvisionné au
   début. Au démarrage, la console affiche un bandeau `MAINNET` et Telegram reçoit
   « bot started — mode MAINNET ».

## 6. Exploiter le service

`install.sh` a déjà installé et activé le service : il redémarre seul après un reboot ou
un plantage (30 s plus tard).

| Besoin | Commande |
|--------|----------|
| Suivre le journal | `journalctl -u ntfal-bot -f` |
| Appliquer une modification de `config.toml` ou `.env` | `sudo /opt/ntfal/deploy/install.sh` (revérifie tout, puis redémarre) |
| Mettre à jour le code | `sudo /opt/ntfal/deploy/install.sh --update` (`git pull` puis réinstallation ; tes modifications de `config.toml` sont conservées, ou le script s'arrête en cas de conflit) |
| Pause (plus d'entrée, sorties et stops actifs) | `sudo -u ntfal touch /opt/ntfal/cache/HALT` |
| Reprendre | `rm /opt/ntfal/cache/HALT` |
| Arrêter le bot | `systemctl stop ntfal-bot` (les stops restent sur l'exchange) |
| Rapport immédiat | `sudo -u ntfal /opt/ntfal/.venv/bin/python bot.py --report` (depuis `/opt/ntfal`) |

L'état du bot (positions qu'il possède, entrée en attente, journal des actions) est dans
`cache/execution_state.json`. **Ne le supprime pas pendant qu'une position est ouverte.**
Si c'est arrivé quand même, le bot reprend la gestion de toute position qui porte encore
un de ses stops sur l'exchange.

## 7. Sécuriser le VPS

- `sudo ./deploy/install.sh --harden` installe un pare-feu qui n'ouvre que SSH (`ufw`),
  `fail2ban` et les mises à jour de sécurité automatiques (`unattended-upgrades`). Si ton
  SSH n'écoute pas sur le port 22, ouvre d'abord ce port à la main, sinon tu te
  bloquerais dehors.
- Connexion SSH par clé uniquement : dans `/etc/ssh/sshd_config`, mets
  `PasswordAuthentication no`, puis `systemctl reload ssh`. Fais-le seulement après avoir
  vérifié que ta connexion par clé fonctionne.
- **N'expose pas le tableau de bord** sur Internet. Le service écoute sur 127.0.0.1 :
  passe par le tunnel SSH de la section 4.
- `.env` est en `chmod 600`, propriété de `ntfal`. Le code appartient à root : le service
  ne peut écrire que dans `cache/`. Le token Telegram n'apparaît jamais dans les journaux.

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
