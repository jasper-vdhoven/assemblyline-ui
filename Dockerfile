FROM cccs/assemblyline:4.0.0.dev15

ARG version

# Switch to root to install dependancies
USER root

# Install assemblyline UI
RUN pip3 install assemblyline-ui==$version

# Switch back to assemblyline and run the app
USER assemblyline
CMD ["gunicorn", "al_ui.app:app", "--config=/usr/local/lib/python3.7/site-packages/al_ui/gunicorn_config.py"]