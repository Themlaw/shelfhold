# Lire hors ligne

Cette page s'adresse à ceux qui lisent dans le lecteur de Shelfhold, installé comme application
(« Ajouter à l'écran d'accueil »). Elle dit ce que « Lire hors ligne » garde sur l'appareil, pour qui,
et ce qui l'efface.

## Ce qui est gardé, et pour qui

- **Lire hors ligne** garde le livre **sur cet appareil**, pour **le profil connecté** : ses pages,
  et de quoi ouvrir le lecteur sans réseau. Le bouton est **dans le lecteur**, ouvert par chaque
  profil — le membre, l'enfant, l'administrateur ; la fiche d'une série l'offre aussi à
  l'administrateur. Seul un livre qui se lit en entier se garde ; un extrait ne se garde jamais.
- La copie est **à vous seul**. Sur un appareil partagé, quelqu'un d'autre qui se connecte ne voit
  rien de ce que vous avez gardé : ses copies sont effacées quand le profil change, pas seulement
  cachées. **Copies hors ligne** (sur la fiche, ou `/hors-ligne`) liste celles de votre profil, et
  la liste suit ce qui arrive aux copies — même gardées ou retirées dans un autre onglet.
- Une page lue hors ligne retient **où vous en êtes**. Au retour du réseau, la position est envoyée
  avec l'heure à laquelle vous l'avez lue : si vous avez lu ailleurs entre-temps, la plus récente
  l'emporte et l'autre entre dans l'historique. Si le serveur est occupé, la position attend et repart
  un peu plus tard ; elle n'est jamais jetée.

## Ce qui l'efface

- **Se déconnecter** efface toutes les copies du profil, avant même de prévenir le serveur : on peut
  se déconnecter sans réseau.
- **Changer de profil** sur l'appareil efface celles du profil précédent.
- Au retour du réseau, une œuvre **supprimée, purgée ou devenue invisible** pour votre profil
  (reclassement, étagère retirée) est retirée de l'appareil. Le serveur ne dit rien d'une œuvre que
  votre profil n'a jamais pu voir.
- Une session ou un jeton révoqué, ou un profil qui perd l'accès, efface tout au premier retour du
  réseau.
- **Retirer la copie** (fiche ou `/hors-ligne`) l'efface tout de suite.

## Ce qui ne s'efface pas (risque accepté R66)

Tant que l'appareil n'a pas de réseau, rien ne peut lui retirer une copie : un livre reclassé pendant
ce temps reste lisible jusqu'à la reconnexion — et c'est vrai pour chaque profil, l'enfant comme les
adultes. Et un fichier **téléchargé par OPDS ou par `/lite`**, ou copié dans une liseuse, est sorti de
Shelfhold : il ne peut pas être repris, comme avec Kavita. Seules les copies du lecteur installé sont
sous contrôle. Pour un appareil d'enfant, passez par ces copies et par les jetons d'appareil
révocables, plutôt que par un téléchargement.
