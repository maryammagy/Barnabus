# Reproducibility Manifest

**Document ID:** reproducibility-v1  
**Date:** 2026-08-31  

---

## 1. Analysis Provenance

### 1.1 Analysis Identification

- **Analysis ID:** 228f930b076eb4c5495ac2b84066ba658b91a1c62a1302fc32a4f1bc96f8cdea
- **Scientific Supplement ID:** 3741598adc31cfdc61c050be5b97e2de93154823e6d859298456a73fb2d4125a
- **Code Commit:** See manifest.json

### 1.2 Data Sources

| Source | File | Hash |
|--------|------|------|
| Events | events/*.parquet | See manifest |
| Snapshot | snapshot_cases.csv | See manifest |
| Model Scores | model_scores.csv | See manifest |
| Labels | labels_pairs.csv | See manifest |

### 1.3 Configuration

- Analysis config: `config/analysis-plan-v1.yaml`
- Monitoring config: `config/monitoring-service-v1.yaml`
- Scientific config: `config/scientific-supplement-v1.yaml`

---

## 2. Execution Commands

### 2.1 Full Pipeline

```bash
docker compose run --rm --build pipeline
```

### 2.2 Analysis

```bash
docker compose run --rm analysis
```

### 2.3 Services

```bash
docker compose up --build evaluation-monitoring analytics-assistant
```

### 2.4 Tests

```bash
docker compose run --rm test
```

---

## 3. Verification

### 3.1 Hash Verification

All artifacts are content-addressed and hash-verified:
- 1,440 numeric cells registered
- 27 artifacts verified
- All SVG figures parse

### 3.2 Idempotent Rerun

Immediate rerun returns identical analysis ID.

---

## 4. Limitations

### 4.1 Sealed Scale

Sealed runtime and memory are not proven:
- Local data: ~1.9M rows, ~115 seconds
- Sealed: ~400M rows, expected <40 minutes

### 4.2 Docker Execution

Local Docker Linux engine was unavailable:
- Services cannot be run in current environment
- Code is verified but runtime is not

---

## 5. Dependencies

### 5.1 Python Packages

All dependencies are pinned in requirements.txt:
- duckdb
- pandas
- numpy
- scipy
- pyyaml
- pytest

### 5.2 System

- Python 3.12
- Docker
- Git

---

## 6. No Manual Calculation

**Every number in this analysis comes from a locked script.**

No notebook, spreadsheet, or manual calculation is in the production path.

---

*This manifest documents the reproducibility of the locked analysis.*
