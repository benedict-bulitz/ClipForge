"""YouTube Learning Loop V1: connect, upload privately, schedule, measure, learn.

Modules: ``provider`` (the Google HTTP boundary), ``connection`` (the single
connection authority), ``uploads`` (the single project/revision/video
mapping and uploader), ``analytics`` (the single analytics store),
``fingerprint`` (immutable production decisions), ``learning`` (derived,
read-only signals; nothing here modifies generation rules) and ``library``
(the project-independent Video Library read model over all of the above).
"""
