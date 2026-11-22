# Same base image the organizers labeled with.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# sqlglot's own Python sources at the pinned commit eb11aec4 (MIT, see SQLGLOT-LICENSE),
# so the selector can compare each patch against the original code.
COPY sqlglot-src.tar.gz /tmp/sqlglot-src.tar.gz
RUN mkdir -p /opt/sqlglot && tar -xzf /tmp/sqlglot-src.tar.gz -C /opt/sqlglot && rm /tmp/sqlglot-src.tar.gz
COPY selector.py map.json map_ids.json attr_reads.json /app/
RUN python -c "import compileall; compileall.compile_file('/app/selector.py', quiet=1); compileall.compile_dir('/opt/sqlglot/sqlglot', quiet=1)" && chmod -R a+rX /app /opt/sqlglot
# Which sqlglot modules fail to import on the pinned source, found with the same
# check the selector runs on a patch, so each run need not repeat it. Built as
# nobody, like the run, so a module that only imports with more rights is not
# counted as a baseline failure here.
RUN setpriv --reuid=65534 --regid=65534 --clear-groups env -i HOME=/tmp PATH=/usr/local/bin:/usr/bin:/bin python -S -c "import sys; sys.path.insert(0, '/app'); import selector; selector.BASELINE_FILE = __import__('pathlib').Path('/tmp/import_baseline.json'); selector.write_baseline()" && cp /tmp/import_baseline.json /app/import_baseline.json && rm /tmp/import_baseline.json && chmod a+r /app/import_baseline.json
CMD ["python", "-S", "-c", "import sys; sys.path.insert(0, '/app'); import selector; selector.main()"]
