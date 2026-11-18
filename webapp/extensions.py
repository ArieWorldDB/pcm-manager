"""Flask extensions initialization"""
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_bcrypt import Bcrypt
from flask_wtf.csrf import CSRFProtect
from flask_marshmallow import Marshmallow
from celery import Celery
from celery.schedules import crontab
import os

# Initialize extensions (will be bound to app in create_app)
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
bcrypt = Bcrypt()
csrf = CSRFProtect()
ma = Marshmallow()

# Initialize Celery with broker URL
celery = Celery(
    'pcm_manager',
    broker=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    backend=os.getenv('REDIS_URL', 'redis://localhost:6379/0')
)


def init_extensions(app):
    """Initialize Flask extensions"""
    
    # SQLAlchemy
    db.init_app(app)
    
    # Migrations
    migrate.init_app(app, db)
    
    # Login Manager
    login_manager.init_app(app)
    login_manager.login_view = 'web.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'info'
    
    # Bcrypt
    bcrypt.init_app(app)
    
    # CSRF Protection
    csrf.init_app(app)
    
    # Marshmallow
    ma.init_app(app)
    
    # Celery
    celery_config = dict(app.config)
    # Celery 5.6 rejects mixing the legacy ``CELERY_*`` names in Flask's
    # config with the modern setting names used by the Celery instance.
    celery_config.pop('CELERY_BROKER_URL', None)
    celery_config.pop('CELERY_RESULT_BACKEND', None)
    celery.conf.update(celery_config)
    celery.conf.update(
        broker_url=app.config.get('REDIS_URL', 'redis://localhost:6379/0'),
        result_backend=app.config.get('REDIS_URL', 'redis://localhost:6379/0'),
    )
    celery.conf.timezone = 'Europe/Amsterdam'
    celery.conf.beat_schedule = {
        **(celery.conf.beat_schedule or {}),
        'sync-pcs-national-race-calendar-daily': {
            'task': 'webapp.tasks.sync_pcs_national_race_calendar',
            'schedule': crontab(hour=4, minute=15),
        },
        'start-pcs-shared-results-daily': {
            'task': 'webapp.tasks.start_pcs_shared_results_daily',
            'schedule': crontab(hour=19, minute=30),
        },
        'check-pcs-unconfirmed-slugs-daily': {
            'task': 'webapp.tasks.check_pcs_unconfirmed_slugs_daily',
            'schedule': crontab(hour=0, minute=30),
        },
        'recover-pcs-shared-results': {
            'task': 'webapp.tasks.recover_pcs_shared_results',
            'schedule': app.config.get('PCS_SYNC_RECOVERY_INTERVAL_SECONDS', 5.0),
        },
    }
    
    class ContextTask(celery.Task):
        """Make celery tasks work with Flask app context"""
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    
    celery.Task = ContextTask
