# SPDX-License-Identifier: GPL-3.0-or-later
from setuptools import setup, find_packages

setup(
    name='iib',
    version='8.7.7',
    long_description=__doc__,
    packages=find_packages(exclude=['tests', 'tests.*']),
    include_package_data=True,
    zip_safe=False,
    install_requires=[
        'boto3',
        'celery',
        'dogpile.cache',
        'flask',
        'flask-login',
        'flask-migrate',
        'flask-sqlalchemy',
        'importlib-resources',
        'operator-manifest',
        'psycopg2-binary',
        'python-memcached ',
        'python-qpid-proton==0.38.0',
        'requests',
        'requests-kerberos',
        'ruamel.yaml',
        'ruamel.yaml.clib',
        'tenacity',
        'typing-extensions',
        'packaging',
        'opentelemetry-api',
        'opentelemetry-sdk',
        'opentelemetry-exporter-otlp',
        'opentelemetry-instrumentation-flask',
        'opentelemetry-instrumentation',
        'opentelemetry-instrumentation-wsgi',
        'opentelemetry-instrumentation-sqlalchemy',
        'opentelemetry-instrumentation-celery',
        'opentelemetry-instrumentation-requests',
        'opentelemetry-instrumentation-logging',
        'opentelemetry-instrumentation-botocore',
    ],
    classifiers=[
        'License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
    ],
    entry_points={'console_scripts': ['iib=iib.web.manage:cli']},
    license="GPLv3+",
    python_requires='>=3.8',
)
