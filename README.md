# EnergyConsumtion - Benchmark JMH sur Grid5000

Projet de benchmarking Java (JMH + Eclipse Collections) avec mesure de consommation énergétique, exécuté sur le cluster **Taurus** de Grid5000.

## Prérequis

| Outil | Version minimale |
|---|---|
| **Java (JDK)** | 17 |
| **Python** | 3.8+ |
| **pip** | — |

```bash
pip install matplotlib pandas
```

> Gradle est embarqué via le Gradle Wrapper (`gradlew`), aucune installation séparée n'est nécessaire.

---

## Déploiement sur Grid5000 (cluster Taurus - Lyon)

### Étape 1 : Réserver 3 nœuds Taurus

```bash
oarsub -I -l nodes=3,walltime=12:00:00 -p "cluster='taurus'" -t monitor='wattmetre_power_watt'
```

### Étape 2 : Récupérer les noms des nœuds alloués

```bash
N1=$(sort -u "$OAR_NODEFILE" | sed -n '1p' | cut -d. -f1)
N2=$(sort -u "$OAR_NODEFILE" | sed -n '2p' | cut -d. -f1)
N3=$(sort -u "$OAR_NODEFILE" | sed -n '3p' | cut -d. -f1)

echo "$N1"
echo "$N2"
echo "$N3"
```

### Étape 3 : Cloner le dépôt sur chaque nœud

Trois dossiers séparés sont nécessaires pour éviter que les versions s'écrasent mutuellement leurs données.

```bash
oarsh "$N1" "git clone --branch main https://github.com/SaraMez/EnergyConsumtion.git ~/TB_R1"
oarsh "$N2" "git clone --branch main https://github.com/SaraMez/EnergyConsumtion.git ~/TB_R2"
oarsh "$N3" "git clone --branch main https://github.com/SaraMez/EnergyConsumtion.git ~/TB_R3"
```

### Étape 4 : Préparer l'environnement sur chaque nœud

Cette étape corrige les fins de ligne de `gradlew`, lui donne les droits d'exécution, et supprime tout verrou JMH résiduel.

```bash
oarsh "$N1" "cd ~/TB_R1 && sed -i 's/\r$//' gradlew && chmod +x gradlew && rm -f /tmp/jmh.lock"
oarsh "$N2" "cd ~/TB_R2 && sed -i 's/\r$//' gradlew && chmod +x gradlew && rm -f /tmp/jmh.lock"
oarsh "$N3" "cd ~/TB_R3 && sed -i 's/\r$//' gradlew && chmod +x gradlew && rm -f /tmp/jmh.lock"
```

### Étape 5 : Lancer les benchmarks

Chaque nœud prend en charge un sous-ensemble de versions d'Eclipse Collections. Les runs sont lancés **un par un** : après avoir envoyé la commande sur un nœud, appuyez sur **Ctrl+Maj+C** pour en sortir, puis passez au suivant.

**Nœud 1 : versions 7.1.2, 8.1.0, 8.2.0**

```bash
oarsh "$N1" "cd ~/TB_R1 && nohup python3 run_simple.py \
  --versions 7.1.2 8.1.0 8.2.0 \
  --site lyon \
  --node \$(hostname -s) \
  --job-id $OAR_JOB_ID \
  --metrics wattmetre_power_watt \
  --includes 'benchmark.(List|Map|Set|Bag).*' \
  --run-count 2 \
  --iterations 5 \
  --warmup-iterations 2 \
  --forks 1 \
  --iteration-time 1s \
  --warmup-time 1s \
  --idle-seconds 30 \
  --kwollect-settle-seconds 10 \
  --inter-iteration-seconds 15 \
  --rest-seconds 10 \
  > run-r1.log 2>&1 < /dev/null &"
```

**Nœud 2 : versions 9.2.0, 10.2.0, 10.4.0**

```bash
oarsh "$N2" "cd ~/TB_R2 && nohup python3 run_simple.py \
  --versions 9.2.0 10.2.0 10.4.0 \
  --site lyon \
  --node \$(hostname -s) \
  --job-id $OAR_JOB_ID \
  --metrics wattmetre_power_watt \
  --includes 'benchmark.(List|Map|Set|Bag).*' \
  --run-count 2 \
  --iterations 5 \
  --warmup-iterations 2 \
  --forks 1 \
  --iteration-time 1s \
  --warmup-time 1s \
  --idle-seconds 30 \
  --kwollect-settle-seconds 10 \
  --inter-iteration-seconds 15 \
  --rest-seconds 10 \
  > run-r2.log 2>&1 < /dev/null &"
```

**Nœud 3 : versions 11.0.0, 11.1.0, 12.0.0, 13.0.0**

```bash
oarsh "$N3" "cd ~/TB_R3 && nohup python3 run_simple.py \
  --versions 11.0.0 11.1.0 12.0.0 13.0.0 \
  --site lyon \
  --node \$(hostname -s) \
  --job-id $OAR_JOB_ID \
  --metrics wattmetre_power_watt \
  --includes 'benchmark.(List|Map|Set|Bag).*' \
  --run-count 2 \
  --iterations 5 \
  --warmup-iterations 2 \
  --forks 1 \
  --iteration-time 1s \
  --warmup-time 1s \
  --idle-seconds 30 \
  --kwollect-settle-seconds 10 \
  --inter-iteration-seconds 15 \
  --rest-seconds 10 \
  > run-r3.log 2>&1 < /dev/null &"
```

### Étape 6 : Surveiller les logs

```bash
oarsh "$N1" "tail -f ~/TB_R1/run-r1.log"
oarsh "$N2" "tail -f ~/TB_R2/run-r2.log"
oarsh "$N3" "tail -f ~/TB_R3/run-r3.log"
```

---

## Description des arguments de `run_simple.py`

| Argument | Exemple | Description |
|---|---|---|
| `--versions` | `11.1.0 12.0.0` | Versions d'Eclipse Collections à benchmarker |
| `--site` | `lyon` | Site Grid5000 (métadonnées des résultats) |
| `--node` | `$(hostname -s)` | Nom court du nœud alloué |
| `--job-id` | `$OAR_JOB_ID` | Identifiant du job OAR |
| `--metrics` | `wattmetre_power_watt` | Métrique kwollect à collecter |
| `--includes` | `'benchmark.(List\|Map\|Set\|Bag).*'` | Regex JMH pour filtrer les benchmarks |
| `--run-count` | `2` | Nombre de répétitions du run complet |
| `--iterations` | `5` | Nombre d'itérations de mesure JMH |
| `--warmup-iterations` | `2` | Nombre d'itérations de chauffe JMH |
| `--forks` | `1` | Nombre de JVM forkées par benchmark |
| `--iteration-time` | `1s` | Durée de chaque itération de mesure |
| `--warmup-time` | `1s` | Durée de chaque itération de chauffe |
| `--idle-seconds` | `30` | Fenêtre d'inactivité avant chaque run (mesure idle) |
| `--kwollect-settle-seconds` | `10` | Délai de stabilisation de kwollect |
| `--inter-iteration-seconds` | `15` | Pause entre deux itérations |
| `--rest-seconds` | `10` | Pause entre deux runs successifs |

---

## Sorties

Les résultats sont déposés dans `build/campagne/` sur chaque nœud :

- **Fichier JSON** données brutes par version et par itération
