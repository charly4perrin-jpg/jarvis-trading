# Sécurité

Pour signaler une faille, ouvre une alerte privée via l'onglet *Security* du dépôt GitHub (« Report a vulnerability »). N'ouvre pas d'issue publique.

Principes du projet : fonctionnement *fail-closed* (dans le doute, rien ne part), journal chaîné SHA-256, règles de risque fixes et non modifiables par les agents, clés API uniquement dans `.env` (jamais versionné), aucune méthode d'ordre réel, de dépôt ou de retrait.
