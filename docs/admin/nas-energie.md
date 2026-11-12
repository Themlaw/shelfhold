# NAS : veille des disques et énergie

Un NAS met souvent ses disques en veille. Ce qui compte pour Shelfhold :

- **`/data` sur un SSD ou un disque qui ne dort pas** : la base est écrite en continu ; une veille provoque des
  attentes, pas de perte, mais des temps de réponse longs au réveil.
- **Un disque de bibliothèque qui dort** : le premier accès attend le réveil. Un scan de Kavita sur un disque
  en veille peut paraître figé ; attendez.
- **Disque absent au démarrage** : si un volume n'est pas monté, Shelfhold met l'écriture en pause (sentinelle
  `.shelfhold-mount-id`) au lieu d'écrire dans un dossier vide. Voir [restauration.md](restauration.md).
- **Arrêter Suwayomi ou Kavita pour économiser** : [reveil-sablier.md](reveil-sablier.md) (Sablier, mode piloté).
- **Coupure de courant** : les écritures sont atomiques et journalisées ; relancez, puis `shelfhold status`.

Gardez le conteneur en `restart: unless-stopped` et la sauvegarde ([sauvegarde.md](sauvegarde.md)) sur un autre
disque que `/data`.
