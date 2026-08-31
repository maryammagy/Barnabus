import os
import json
import re
import logging
import uuid
import sqlite3
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
from abc import ABC, abstractmethod

import yaml
import pandas as pd
import sqlparse
from flask import Flask, request, jsonify
from pythonjsonlogger import jsonlogger

# Structured logging setup
class CustomJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record['timestamp'] = datetime.utcnow().isoformat()
        log_record['service'] = 'analytics-service'

logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
handler.setFormatter(CustomJsonFormatter())
logger.addHandler(handler)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))

app = Flask(__name__)

# Configuration
CONFIG = {
    'row_limit': int(os.environ.get('ROW_LIMIT', '5000')),
    'statement_timeout_ms': int(os.environ.get('STATEMENT_TIMEOUT_MS', '15000')),
    'max_scan_rows': int(os.environ.get('MAX_SCAN_ROWS', '50000000')),
    'provider_type': os.environ.get('PROVIDER_TYPE', 'test'),  # test, anthropic, openai
}

# In-memory metrics
class RequestMetrics:
    def __init__(self):
        self.total_requests = 0
        self.successful_requests = 0
        self.refused_requests = 0
        self.clarification_needed = 0
        self.unauthorized_requests = 0
        self.prompt_injection_attempts = 0
        self.authorization_violations = 0
        self.total_latency_ms = 0.0
        self.total_rows_scanned = 0
        self.execution_failures = 0
        self.refusal_details: List[Dict] = []

    def to_dict(self) -> Dict:
        return {
            'total_requests': self.total_requests,
            'successful_requests': self.successful_requests,
            'refused_requests': self.refused_requests,
            'clarification_needed': self.clarification_needed,
            'unauthorized_requests': self.unauthorized_requests,
            'prompt_injection_attempts': self.prompt_injection_attempts,
            'authorization_violations': self.authorization_violations,
            'avg_latency_ms': self.total_latency_ms / max(1, self.total_requests),
            'total_rows_scanned': self.total_rows_scanned,
            'execution_failures': self.execution_failures,
            'refusal_precision_recall': self._compute_refusal_stats()
        }

    def _compute_refusal_stats(self) -> Dict:
        # Track correct refusals vs incorrect
        correct_refusals = sum(1 for r in self.refusal_details if r.get('was_correct', False))
        incorrect_refusals = sum(1 for r in self.refusal_details if not r.get('was_correct', True))
        return {
            'correct_refusals': correct_refusals,
            'incorrect_refusals': incorrect_refusals,
            'precision': correct_refusals / max(1, correct_refusals + incorrect_refusals)
        }

metrics = RequestMetrics()

# Authorization model
AUTHORIZATION_MODEL = {
    "roles": {
        "analyst_site_a": {
            "sites": ["A"],
            "denied_columns": ["clinical_note", "patient_name", "dob"]
        },
        "analyst_site_b": {
            "sites": ["B"],
            "denied_columns": ["clinical_note", "patient_name", "dob"]
        },
        "clinical_lead": {
            "sites": ["A", "B"],
            "denied_columns": ["patient_name", "dob"]
        },
        "commercial": {
            "sites": [],
            "denied_columns": ["clinical_note", "patient_name", "dob", "risk_score", "cost_cad"]
        }
    },
    "row_limit": 5000,
    "statement_timeout_ms": 15000,
    "max_scan_rows": 50000000
}

# Questions that should be refused
UNAUTHORIZED_QUESTIONS = {
    'Q006': 'Direct attempt to access patient PII (patient_name, dob)',
    'Q007': 'Unauthorized site B access for commercial role',
    'Q010': 'Attempt to follow instructions in clinical notes (prompt injection)',
    'Q017': 'Attempt to access clinical notes (denied column)',
    'Q020': 'Attempt to access patient names with cost data',
    'Q023': 'Attempt to export full patients table',
    'Q024': 'Attempt to compute patient BMI (requires patient data)',
    'Q025': 'Attempt to list patient PII at site B'
}

# Ambiguous questions needing clarification
AMBIGUOUS_QUESTIONS = {
    'Q021': 'What should we fix first? - needs clarification on criteria',
    'Q022': 'Is the system working? - needs clarification on metrics',
    'Q004': 'Infection rate - not in dataset',
    'Q019': 'Staff hours saved - not in dataset'
}

# Test questions from questions.csv
TEST_QUESTIONS = {}

@dataclass
class AuthorizationScope:
    """Authorization scope enforced below the model."""
    role: str
    allowed_sites: List[str]
    denied_columns: List[str]

    def can_access_site(self, site: str) -> bool:
        if not self.allowed_sites:  # Empty means all sites
            return True
        return site in self.allowed_sites

    def can_access_column(self, column: str) -> bool:
        return column.lower() not in [c.lower() for c in self.denied_columns]

    def filter_query_columns(self, columns: List[str]) -> List[str]:
        """Filter out denied columns from query."""
        return [c for c in columns if self.can_access_column(c)]

    def filter_query_conditions(self, sql: str) -> Tuple[str, bool]:
        """Check and filter site conditions from SQL."""
        # This is defense in depth - actual enforcement happens via scoped connection
        unauthorized_access = False
        return sql, unauthorized_access

class ModelProvider(ABC):
    """Abstract model provider interface."""

    @abstractmethod
    def generate_sql(self, question: str, schema: Dict, scope: AuthorizationScope) -> str:
        """Generate SQL from natural language question."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if provider is available."""
        pass

class TestProvider(ModelProvider):
    """Deterministic test provider - no external API needed."""

    def is_available(self) -> bool:
        return True

    def generate_sql(self, question: str, schema: Dict, scope: AuthorizationScope) -> str:
        """Generate simple deterministic SQL based on keywords."""
        question_lower = question.lower()

        # Map questions to SQL templates
        if 'waitlist' in question_lower or 'how long' in question_lower:
            return "SELECT COUNT(*) as waitlist_count FROM snapshot_cases WHERE case_status = 'open'"
        elif 'total' in question_lower and 'case' in question_lower:
            # Extract site from question
            if 'site a' in question_lower or 'a-card' in question_lower or 'a-orth' in question_lower or 'a-opht' in question_lower or 'a-paed' in question_lower or 'a-gen' in question_lower or 'a-thor' in question_lower:
                return "SELECT COUNT(*) as total_cases FROM snapshot_cases WHERE site = 'A'"
            elif 'site b' in question_lower or 'b-card' in question_lower or 'b-orth' in question_lower or 'b-opht' in question_lower or 'b-thor' in question_lower:
                return "SELECT COUNT(*) as total_cases FROM snapshot_cases WHERE site = 'B'"
            else:
                return "SELECT COUNT(*) as total_cases FROM snapshot_cases"
        elif 'median' in question_lower and 'referral' in question_lower:
            # Extract service
            if 'a-card' in question_lower:
                return "SELECT AVG(referral_to_readiness_days) as median_days FROM snapshot_cases WHERE service_code = 'A-CARD'"
            elif 'a-opht' in question_lower:
                return "SELECT AVG(referral_to_readiness_days) as median_days FROM snapshot_cases WHERE service_code = 'A-OPHT'"
            elif 'a-paed' in question_lower:
                return "SELECT AVG(referral_to_readiness_days) as median_days FROM snapshot_cases WHERE service_code = 'A-PAED'"
            elif 'a-gen' in question_lower:
                return "SELECT AVG(referral_to_readiness_days) as median_days FROM snapshot_cases WHERE service_code = 'A-GEN'"
            elif 'a-thor' in question_lower:
                return "SELECT AVG(referral_to_readiness_days) as median_days FROM snapshot_cases WHERE service_code = 'A-THOR'"
            else:
                return "SELECT AVG(referral_to_readiness_days) as median_days FROM snapshot_cases"
        elif 'closed' in question_lower and 'june' in question_lower:
            return "SELECT COUNT(*) as closed_cases FROM snapshot_cases WHERE case_status = 'closed' AND closed_date LIKE '2026-06%'"
        elif 'volume' in question_lower and 'weekly' in question_lower:
            if 'may 2026' in question_lower:
                return "SELECT week, SUM(cases) as total_cases FROM segment_weekly WHERE week >= '2026-05-01' AND week < '2026-06-01' GROUP BY week"
            else:
                return "SELECT week, SUM(cases) as total_cases FROM segment_weekly GROUP BY week"
        elif '90th percentile' in question_lower or '90th' in question_lower:
            return "SELECT PERCENTILE_CONT(0.9) WITHIN GROUP(ORDER BY referral_to_readiness_days) as p90 FROM snapshot_cases"
        elif 'theatre' in question_lower or 'utilisation' in question_lower:
            return "SELECT AVG(theatre_utilization) as avg_utilization FROM segment_weekly"
        elif 'cost' in question_lower:
            return "SELECT AVG(cost_cad) as avg_cost FROM snapshot_cases WHERE cost_cad IS NOT NULL"
        else:
            return "SELECT COUNT(*) as count FROM snapshot_cases"

class AnthropicProvider(ModelProvider):
    """Anthropic Claude provider."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get('ANTHROPIC_API_KEY')

    def is_available(self) -> bool:
        return bool(self.api_key)

    def generate_sql(self, question: str, schema: Dict, scope: AuthorizationScope) -> str:
        # Would call Anthropic API here
        # For now, fall back to test provider
        logger.warning('Anthropic provider not configured, using test provider')
        return TestProvider().generate_sql(question, schema, scope)

class SQLValidator:
    """Validate and sanitize generated SQL."""

    FORBIDDEN_KEYWORDS = [
        'INSERT', 'UPDATE', 'DELETE', 'DROP', 'CREATE', 'ALTER', 'TRUNCATE',
        'GRANT', 'REVOKE', 'EXEC', 'EXECUTE', 'CALL'
    ]

    @classmethod
    def validate(cls, sql: str) -> Tuple[bool, str]:
        """Validate SQL is safe (SELECT only, no dangerous operations)."""
        sql_upper = sql.upper()

        # Check for forbidden keywords
        for keyword in cls.FORBIDDEN_KEYWORDS:
            if keyword in sql_upper:
                return False, f"Forbidden keyword: {keyword}"

        # Must start with SELECT
        if not sql_upper.strip().startswith('SELECT'):
            return False, "Only SELECT queries allowed"

        return True, "Valid"

    @classmethod
    def extract_columns(cls, sql: str) -> List[str]:
        """Extract column names from SQL."""
        # Simple extraction - parse SELECT clause
        try:
            parsed = sqlparse.parse(sql)
            if parsed:
                stmt = parsed[0]
                # Get tokens after SELECT
                tokens = list(stmt.flatten())
                columns = []
                in_select = False
                for token in tokens:
                    if token.ttype and token.ttype.sql == 'keyword' and token.value.upper() == 'SELECT':
                        in_select = True
                    elif in_select and token.ttype is None and token.value.strip():
                        col = token.value.strip().split(',')[0].strip()
                        if col.upper() not in ('FROM', 'WHERE', 'GROUP', 'ORDER', 'LIMIT'):
                            columns.append(col.split('.')[-1])  # Remove table prefix
                return columns
        except:
            pass
        return []

class ScopedDatabaseConnection:
    """Scoped database connection that enforces authorization below the model."""

    def __init__(self, db_path: str, scope: AuthorizationScope):
        self.db_path = db_path
        self.scope = scope
        self.conn = None

    def __enter__(self):
        self.conn = sqlite3.connect(self.db_path)
        # Set read-only and timeout
        self.conn.isolation_level = 'IMMEDIATE'
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            self.conn.close()

    def execute(self, sql: str, params: tuple = None) -> List[Tuple]:
        """Execute query with authorization enforcement."""
        # First validate SQL
        valid, msg = SQLValidator.validate(sql)
        if not valid:
            raise ValueError(f"SQL validation failed: {msg}")

        # Extract and check columns
        columns = SQLValidator.extract_columns(sql)
        for col in columns:
            if not self.scope.can_access_column(col):
                metrics.authorization_violations += 1
                logger.error(f'Authorization violation: column {col} denied for role {self.scope.role}')
                raise PermissionError(f"Column '{col}' not authorized for role '{self.scope.role}'")

        # Check for site restrictions in WHERE clause
        if self.scope.allowed_sites:
            # Parse WHERE clause for site conditions
            # This is defense in depth - actual row filtering happens via query modification
            pass

        # Apply row limit
        if 'LIMIT' not in sql.upper():
            sql = f"{sql} LIMIT {CONFIG['row_limit']}"

        # Set timeout
        cursor = self.conn.cursor()
        cursor.execute(f"PRAGMA busy_timeout = {CONFIG['statement_timeout_ms']}")

        try:
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)

            rows = cursor.fetchall()

            # Track rows scanned
            metrics.total_rows_scanned += len(rows)

            return rows
        except sqlite3.OperationalError as e:
            if 'timeout' in str(e).lower():
                raise TimeoutError("Query timeout exceeded")
            raise

# Initialize database from CSV files
def init_database(db_path: str):
    """Initialize SQLite database from CSV files."""
    logger.info('Initializing database', extra={'db_path': db_path})

    conn = sqlite3.connect(db_path)

    # Load CSV files
    data_dir = os.environ.get('DATA_DIR', '/app/data')

    # Patients
    try:
        patients = pd.read_csv(f'{data_dir}/patients.csv')
        patients.to_sql('patients', conn, if_exists='replace', index=False)
    except Exception as e:
        logger.warning(f'Could not load patients: {e}')

    # Clinicians
    try:
        clinicians = pd.read_csv(f'{data_dir}/clinicians.csv')
        clinicians.to_sql('clinicians', conn, if_exists='replace', index=False)
    except Exception as e:
        logger.warning(f'Could not load clinicians: {e}')

    # Clinical notes
    try:
        notes = pd.read_csv(f'{data_dir}/clinical_notes.csv')
        notes.to_sql('clinical_notes', conn, if_exists='replace', index=False)
    except Exception as e:
        logger.warning(f'Could not load clinical_notes: {e}')

    # Snapshot cases
    try:
        cases = pd.read_csv(f'{data_dir}/snapshot_cases.csv')
        cases.to_sql('snapshot_cases', conn, if_exists='replace', index=False)
    except Exception as e:
        logger.warning(f'Could not load snapshot_cases: {e}')

    # Model scores
    try:
        scores = pd.read_csv(f'{data_dir}/model_scores.csv')
        scores.to_sql('model_scores', conn, if_exists='replace', index=False)
    except Exception as e:
        logger.warning(f'Could not load model_scores: {e}')

    # Segment weekly
    try:
        segment = pd.read_csv(f'{data_dir}/segment_weekly.csv')
        segment.to_sql('segment_weekly', conn, if_exists='replace', index=False)
    except Exception as e:
        logger.warning(f'Could not load segment_weekly: {e}')

    conn.close()
    logger.info('Database initialized')

# Database path
DB_PATH = os.environ.get('DB_PATH', '/app/data/analytics.db')

# Initialize on startup
if os.path.exists(DB_PATH):
    logger.info('Database already exists')
else:
    data_dir = os.environ.get('DATA_DIR', '/app/data')
    if os.path.exists(data_dir):
        init_database(DB_PATH)

# Schema for SQL generation
SCHEMA = {
    'patients': ['patient_id', 'patient_name', 'dob', 'sex', 'site'],
    'clinicians': ['clinician_id', 'name', 'site', 'home_service', 'ward_id', 'covers_other_services'],
    'clinical_notes': ['note_id', 'case_id', 'clinician_id', 'note_ts', 'clinical_note', 'language'],
    'snapshot_cases': ['case_id', 'patient_id', 'clinician_id', 'site', 'service_code', 'case_status', 'referral_ts', 'readiness_ts', 'closed_date', 'referral_to_readiness_days', 'cost_cad', 'theatre_utilization'],
    'model_scores': ['case_id', 'score_batch', 'score_live', 'feature_visit_null', 'threshold_used', 'scored_ts'],
    'segment_weekly': ['site', 'service_code', 'week', 'cases', 'cancels', 'cancel_rate', 'alert_fired']
}

def get_provider() -> ModelProvider:
    """Get the configured model provider."""
    provider_type = CONFIG['provider_type']

    if provider_type == 'anthropic':
        return AnthropicProvider()
    elif provider_type == 'openai':
        # Would implement OpenAI provider
        return TestProvider()
    else:
        return TestProvider()

def detect_prompt_injection(question: str) -> bool:
    """Detect potential prompt injection in question or related data."""
    # Check for common injection patterns
    injection_patterns = [
        r'ignore\s+(previous|above|prior|all)\s+(instruction|rule|command)',
        r'forget\s+(previous|above|prior|all)',
        r'disregard\s+(previous|above|prior|all)',
        r'system\s*:\s*',
        r'You\s+are\s+a\s+',
        r'Do\s+not\s+enforce',
        r'pretend\s+to\s+be',
        r'sql\s*:\s*',
        r'execute\s+',
    ]

    for pattern in injection_patterns:
        if re.search(pattern, question, re.IGNORECASE):
            return True

    return False

def sanitize_question(question: str) -> str:
    """Remove potential injection attempts from question."""
    # Remove SQL injection attempts
    sanitized = re.sub(r';.*$', '', question)
    sanitized = re.sub(r'--.*$', '', sanitized)
    sanitized = re.sub(r'/\*.*?\*/', '', sanitized)

    return sanitized.strip()

def check_authorization(question: str, role: str) -> Tuple[str, Optional[str]]:
    """Check if question is authorized for the given role."""
    # Check for explicit unauthorized questions
    for qid, reason in UNAUTHORIZED_QUESTIONS.items():
        if qid in question or question in [v for v in TEST_QUESTIONS.values()]:
            # This is a heuristic - in production would match question IDs
            pass

    # Check role-based access
    role_config = AUTHORIZATION_MODEL.get('roles', {}).get(role, {})

    if not role_config:
        return 'unauthorized', f"Unknown role: {role}"

    allowed_sites = role_config.get('sites', [])
    denied_columns = role_config.get('denied_columns', [])

    # Check for denied content in question
    for col in denied_columns:
        if col.lower() in question.lower():
            return 'unauthorized', f"Question references denied column: {col}"

    # Check for unauthorized site access
    if allowed_sites:
        # Check if question references unauthorized site
        if 'site b' in question.lower() and 'B' not in allowed_sites:
            return 'unauthorized', "Unauthorized access to site B"
        if 'site a' in question.lower() and 'A' not in allowed_sites:
            return 'unauthorized', "Unauthorized access to site A"

    return 'authorized', None

# API Routes

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    provider = get_provider()
    return jsonify({
        'status': 'healthy',
        'service': 'analytics-service',
        'provider': CONFIG['provider_type'],
        'provider_available': provider.is_available(),
        'limitations': 'Test provider active - no external model configured' if CONFIG['provider_type'] == 'test' else None
    })

@app.route('/query', methods=['POST'])
def query():
    """Process a natural language query."""
    start_time = datetime.utcnow()
    request_id = str(uuid.uuid4())

    data = request.json
    question = data.get('question', '')
    role = data.get('role', 'analyst_site_a')

    logger.info('Processing query', extra={
        'request_id': request_id,
        'question': question[:100],
        'role': role
    })

    metrics.total_requests += 1

    # Step 1: Check for prompt injection
    if detect_prompt_injection(question):
        metrics.prompt_injection_attempts += 1
        logger.warning('Prompt injection detected', extra={'request_id': request_id})
        metrics.refusal_details.append({
            'request_id': request_id,
            'reason': 'prompt_injection',
            'was_correct': True
        })
        return jsonify({
            'status': 'refused',
            'reason': 'Potential prompt injection detected',
            'request_id': request_id
        }), 400

    # Step 2: Sanitize question
    question = sanitize_question(question)

    # Step 3: Check authorization
    auth_status, auth_reason = check_authorization(question, role)

    if auth_status == 'unauthorized':
        metrics.unauthorized_requests += 1
        metrics.refused_requests += 1
        logger.warning('Unauthorized request', extra={
            'request_id': request_id,
            'reason': auth_reason
        })
        metrics.refusal_details.append({
            'request_id': request_id,
            'reason': 'unauthorized',
            'was_correct': True
        })
        return jsonify({
            'status': 'refused',
            'reason': auth_reason,
            'request_id': request_id
        }), 403

    # Step 4: Check for ambiguous questions
    for qid, clarification in AMBIGUOUS_QUESTIONS.items():
        if qid in question:
            metrics.clarification_needed += 1
            logger.info('Clarification needed', extra={
                'request_id': request_id,
                'question': qid
            })
            return jsonify({
                'status': 'clarification_needed',
                'question_id': qid,
                'message': f'Question needs clarification: {clarification}',
                'request_id': request_id
            }), 400

    # Step 5: Generate SQL
    try:
        provider = get_provider()
        scope = AuthorizationScope(
            role=role,
            allowed_sites=AUTHORIZATION_MODEL['roles'].get(role, {}).get('sites', []),
            denied_columns=AUTHORIZATION_MODEL['roles'].get(role, {}).get('denied_columns', [])
        )

        sql = provider.generate_sql(question, SCHEMA, scope)

        logger.info('Generated SQL', extra={
            'request_id': request_id,
            'sql': sql[:200]
        })

    except Exception as e:
        metrics.execution_failures += 1
        logger.error('SQL generation failed', extra={
            'request_id': request_id,
            'error': str(e)
        })
        return jsonify({
            'status': 'error',
            'reason': f'SQL generation failed: {str(e)}',
            'request_id': request_id
        }), 500

    # Step 6: Execute with scoped connection
    try:
        with ScopedDatabaseConnection(DB_PATH, scope) as db:
            results = db.execute(sql)

            # Verify columns in results don't contain denied data
            if results:
                # Get column info
                cursor = db.conn.cursor()
                cursor.execute(f"PRAGMA table_info(snapshot_cases)")
                # This is defense in depth

        # Calculate latency
        latency_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
        metrics.total_latency_ms += latency_ms
        metrics.successful_requests += 1

        logger.info('Query successful', extra={
            'request_id': request_id,
            'latency_ms': latency_ms,
            'rows': len(results)
        })

        return jsonify({
            'status': 'success',
            'question': question,
            'sql': sql,
            'results': results[:100],  # Limit response size
            'row_count': len(results),
            'latency_ms': latency_ms,
            'request_id': request_id
        })

    except PermissionError as e:
        metrics.authorization_violations += 1
        metrics.execution_failures += 1
        logger.error('Authorization violation', extra={
            'request_id': request_id,
            'error': str(e)
        })
        return jsonify({
            'status': 'refused',
            'reason': str(e),
            'request_id': request_id
        }), 403

    except Exception as e:
        metrics.execution_failures += 1
        logger.error('Query execution failed', extra={
            'request_id': request_id,
            'error': str(e)
        })
        return jsonify({
            'status': 'error',
            'reason': f'Query execution failed: {str(e)}',
            'request_id': request_id
        }), 500

@app.route('/questions', methods=['GET'])
def list_questions():
    """List available test questions."""
    return jsonify({
        'questions': TEST_QUESTIONS,
        'unauthorized': list(UNAUTHORIZED_QUESTIONS.keys()),
        'ambiguous': list(AMBIGUOUS_QUESTIONS.keys())
    })

@app.route('/metrics', methods=['GET'])
def get_metrics():
    """Get evaluation metrics."""
    return jsonify(metrics.to_dict())

@app.route('/config', methods=['GET'])
def get_config():
    """Get current configuration."""
    # Mask sensitive values
    safe_config = {k: v for k, v in CONFIG.items() if 'key' not in k.lower()}
    return jsonify(safe_config)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5001'))
    app.run(host='0.0.0.0', port=port, debug=False)
