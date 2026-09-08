# agent-argent

Gestion de portefeuille Binance : suivi, règles de money management, petits
allers-retours automatiques plafonnés, et validation manuelle au-delà du seuil.

**Le bot ne peut pas sortir de fonds de Binance.** Ce n'est pas une politique,
c'est une propriété du code : `withdraw`, `transfer`, `borrow`, `repay`, `redeem`
et `sub-account` sont rejetés sur tout chemin d'API, quelle que soit la liste
blanche, et l'agent refuse de démarrer contre une clé autorisant les retraits.
Quoi que fasse la stratégie, l'argent reste sur le compte.

Deux clients coexistent : `BinanceReadOnlyClient` (suivi seul, refuse une clé qui
peut trader) et `BinanceTradingClient` (ordres spot, refuse une clé qui peut
retirer).

## Ce qu'il fait

| Commande | Effet |
|---|---|
| `verifier` | Contrôle la connexion, les droits de la clé, et leur **cohérence avec votre configuration** |
| `bilan` | Portefeuille valorisé, répartition, variation, repli depuis le plus haut, audit des règles |
| `signal SYMBOLE` | Dimensionne une position pour que toucher le stop coûte exactement le risque configuré |

## Règles de risque (`regles.json`)

| Clé | Défaut | Sens |
|---|---|---|
| `risk_per_trade` | 1 % | Perte encaissée si le stop est touché, en % du capital total |
| `max_position_pct` | 20 % | Plafond de poids d'un actif |
| `max_total_exposure_pct` | 60 % | Plafond d'exposition non-stable |
| `max_drawdown_pct` | 20 % | Repli sous le plus haut au-delà duquel **aucune nouvelle prise de risque n'est proposée** |
| `atr_multiple` | 2.0 | Distance du stop, en multiples d'ATR(14) |

Les plafonds sont validés au chargement : un `risk_per_trade` de 50 % ou un
`max_position_pct` supérieur au plafond d'exposition totale sont rejetés.

Le plus haut historique (*high-water mark*) est persisté dans `state/equity.json`.
Un fichier d'état corrompu fait échouer l'agent au lieu de repartir de zéro —
sinon un repli réel serait effacé et débloquerait les propositions.

## Installation sur le VPS

Aucune dépendance : bibliothèque standard Python 3.10+ uniquement.

```bash
git clone <depot> && cd agent-argent
cp .env.example .env      # puis renseignez les deux clés
python3 -m agent_argent.cli verifier
```

### Créer la clé API Binance

Binance → Profil → **Gestion API** → Créer une API :

1. **Ne cochez pas** « Activer le Trading Spot & Margin ».
2. **Ne cochez pas** « Activer les retraits ».
3. Laissez uniquement la lecture.
4. **Restreignez l'accès aux IP de confiance** (l'IP du VPS). Sans cela Binance
   expire la clé au bout de 90 jours.

`verifier` refusera de démarrer si l'étape 1 ou 2 a été oubliée.

### Secrets

`.env` est dans `.gitignore`. Conformément à la règle 4 d'AMORCE, aucune clé ne
doit apparaître dans le canal, dans un fichier versionné, ni dans un message.

### Bilan quotidien

```cron
0 8 * * * cd /chemin/agent-argent && python3 -m agent_argent.cli bilan >> logs/bilan.log 2>&1
```

`bilan` sort en code 1 quand une règle est en alerte, ce qui permet de conditionner
un envoi. Conformément à la règle 1 d'AMORCE, tout mail se dépose en **brouillon** :
l'agent n'envoie rien de lui-même.

## Mode automatique

Petits allers-retours exécutés seuls, plafonnés ; tout ce qui dépasse le seuil
part en notification et attend votre accord.

```bash
python3 -m agent_argent.cli auto            # simulation, aucun ordre envoyé
python3 -m agent_argent.cli auto --reel     # ordres réels
python3 -m agent_argent.cli attente         # demandes en attente de votre accord
python3 -m agent_argent.cli valider a1b2c3d
python3 -m agent_argent.cli refuser a1b2c3d
```

### Les deux verrous d'armement

Le bot ne place un ordre réel que si **les trois** conditions sont réunies :

1. `"armed": true` dans `autonomous.json`
2. la variable d'environnement `AGENT_ARGENT_ARME` définie sur la machine
3. le drapeau `--reel` sur la ligne de commande

Le verrou 2 est volontairement hors du dépôt : un `"armed": true` commité par
accident reste inerte.

### Les plafonds (`autonomous.json`)

| Clé | Défaut | Effet |
|---|---|---|
| `max_order` | 300 € | Dépense maximale d'un ordre automatique |
| `max_per_asset` | 300 € | Exposition automatique maximale sur une crypto |
| `max_total_auto` | 1500 € | Exposition automatique totale |
| `daily_loss_limit` | 100 € | Perte sur 24 h glissantes → **arrêt** |
| `max_trades_per_day` | 20 | Plafond d'ordres, contre l'emballement |
| `max_open_positions` | 5 | Positions automatiques simultanées |
| `whitelist` | 3 paires | **Rien n'est tradé hors de cette liste** |
| `approval_threshold` | 300 € | Au-delà → notification, pas d'exécution |
| `approval_ttl_minutes` | 30 | Une demande non traitée expire |

Les plafonds sont exprimés en euros et convertis au taux courant, donc 300 €
restent 300 € quoi que fasse le dollar. Ils sont **réévalués juste avant chaque
ordre**, sur des soldes fraîchement lus — jamais sur du cache. Un ordre trop
gros n'est pas rejeté : il est **réduit** pour tenir sous le plafond.

Une validation que vous accordez n'est pas un chèque en blanc : elle expire au
bout de 30 minutes, et l'exécution est annulée si le marché a bougé de plus de
1 % depuis la demande.

### Protection systématique

Chaque entrée est immédiatement suivie d'un OCO (take-profit + stop-loss). Si
l'OCO ne peut pas être placé, **la position est revendue au marché sur-le-champ**.
Une position non protégée est traitée comme un incident, pas comme un état normal.

### Planification

```cron
*/15 * * * * cd /chemin/agent-argent && python3 -m agent_argent.cli auto --reel >> logs/auto.log 2>&1
0 8 * * *    cd /chemin/agent-argent && python3 -m agent_argent.cli bilan >> logs/bilan.log 2>&1
```

Notification : `--notifier "commande"` reçoit le message sur stdin. Une trace est
écrite dans `state/notifications.log` **avant** l'envoi, pour qu'un canal en panne
ne produise jamais une demande silencieuse.

## Avant d'armer : ce que vous devez savoir

La stratégie (`strategy.py`) est un filtre de retour à la moyenne lisible et
auditable. Elle **n'a pas été backtestée sur vos données**, parce qu'il n'y en a
pas encore. Ses paramètres sont mes estimations, pas des mesures.

Faites tourner `auto` sans `--reel` pendant quelques semaines : le journal
(`state/journal.json`) enregistre chaque signal et son prix. On calibrera ensuite
sur du réel. Armer avant cette étape vous donne une machine disciplinée dont
personne, moi compris, ne connaît l'espérance de gain.

## Ce qu'il ne fait pas

- **Aucun retrait, aucun transfert, aucun emprunt.** Interdits en dur sur tout
  chemin d'API, quelle que soit la liste blanche. Le bot peut acheter et vendre ;
  il ne peut pas sortir un euro de Binance.
- Aucun trading sur marge ou sur futures. Spot uniquement.
- Aucun backtest intégré pour l'instant.

## Tests

```bash
python3 -m unittest discover -s tests
```

70 tests couvrent l'impossibilité de sortir des fonds, la valorisation, l'audit
des règles, le dimensionnement, les plafonds du mode automatique, les deux
verrous d'armement, les coupe-circuits et la file de validation.
