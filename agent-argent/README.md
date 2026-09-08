# agent-argent

Suivi de portefeuille Binance **en lecture seule**, avec des règles de money
management explicites et des propositions de dimensionnement à valider à la main.

**Cet agent ne passe aucun ordre, jamais.** Ce n'est pas une politique, c'est une
propriété du code : `agent_argent/binance.py` n'autorise que sept endpoints REST,
tous en lecture, et refuse au démarrage une clé API qui porterait le droit de
trader. Un futur ajout qui tenterait de passer un ordre échoue à la frontière du
client plutôt que d'envoyer de l'argent.

## Ce qu'il fait

| Commande | Effet |
|---|---|
| `verifier` | Contrôle la connexion, la validité de la clé, et **refuse de tourner si la clé peut trader ou retirer** |
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

## Ce qu'il ne fait pas

- Aucune exécution d'ordre, aucun retrait, aucun transfert.
- Aucune prédiction de prix. `signal` répond à « combien », jamais à « quoi » ni
  « quand » : le choix de l'actif reste entièrement le vôtre.
- Aucun backtest pour l'instant.

## Tests

```bash
python3 -m unittest discover -s tests
```

29 tests couvrent la garantie de lecture seule, la valorisation, l'audit des
règles, le dimensionnement et la persistance de l'état.
