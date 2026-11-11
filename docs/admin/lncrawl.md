# Brancher votre instance lncrawl

Shelfhold sait parler à **votre propre instance** lncrawl, celle que vous faites tourner chez vous,
et à aucune autre. Il n'embarque, n'active, ne liste et ne suggère **aucun crawler** : le choix des
sites que votre instance interroge vous appartient, et Shelfhold n'en voit que des identifiants
opaques (un roman, un chapitre, une tâche, un fichier). Le compose fourni ne contient aucun profil
lncrawl.

Ce que reçoit Shelfhold est du contenu **non fiable** : l'EPUB reçu est gardé tel quel comme
original, et seul son dérivé neutralisé (sans script, sans gestionnaire d'événement, sans
référence distante) est rangé dans votre bibliothèque ([EF-BIB-051](../adr/0047-frontiere-de-confiance-du-contenu.md)).

## Brancher l'instance

1. Dans votre instance, créez une clé d'API pour un compte qui ne fait que lire sa bibliothèque et
   lancer des tâches de génération d'EPUB.
2. Réglez l'adresse, puis la clé (voir [sources.md](sources.md)) :

   ```
   shelfhold plugin set lncrawl base_url=http://lncrawl.lan:8080
   shelfhold plugin set-secret lncrawl api_key --secret-file ./cle.txt
   ```

3. Ajoutez `lncrawl` à `SHELFHOLD_ACTIVE_SOURCES`. Tant que ce n'est pas fait, rien ne part.

L'adresse est la seule que Shelfhold contacte : c'est la `DomainRule` du plugin (`base_url`), vérifiée
à chaque saut, et une redirection vers un autre hôte est refusée. Une adresse avec identifiants
(`http://nom:mot@hôte`), un schéma autre que http(s) ou un nom qui ne se résout pas ferment la
règle : aucune requête ne part. La clé est chiffrée dans la base, ajoutée par Shelfhold à chaque
requête vers cette adresse seulement, masquée dans les journaux et jamais écrite dans une référence,
un billet ou un fichier.

## Ce qui se passe

- Les romans suivis sont ceux de **la bibliothèque de votre compte** sur l'instance.
- Shelfhold demande **un EPUB par tome** (une tâche par plage de chapitres, un seul billet, un seul
  `export_item`). Un tome clos n'est jamais redemandé ; le tome en cours est redemandé en entier
  quand un chapitre le rejoint, et remplacé selon la politique de tomes de l'Édition
  (`closed_only` par défaut).
- Un relais d'équipes de traduction reste **une seule Édition** (traduction communautaire) : chaque
  équipe est une Variante, aucune nouvelle Édition à chaque relais.

## Garde-fous

- La seconde étape d'une demande (le fichier) n'est jamais prise sur une adresse que l'instance
  donne : Shelfhold construit l'adresse du fichier à partir de l'identifiant annoncé, sur l'hôte de
  l'instance. Une réponse qui parle d'une autre plage, d'un autre roman ou de chapitres hors de la
  plage demandée est refusée avant le téléchargement.
- Une file qui ne finit jamais ne coûte qu'un nombre borné d'essais : un billet en attente depuis
  plus de 12 h est annulé côté instance, la plage est redemandée au plus 8 fois puis laissée
  (`abandoned`) tant qu'aucun chapitre nouveau ne la rejoint.
- Une réponse trop grosse, qui n'est pas du JSON, dont les listes ne finissent pas ou dont les
  identifiants sortent de l'alphabet `A-Z a-z 0-9 _ -` est un changement de la source, jamais un état.
- Ce que l'instance dit d'elle-même (un nom, une adresse, un message d'erreur) n'est copié ni dans
  un journal, ni dans un événement, ni dans l'interface.
- Un fichier qui n'est pas un conteneur EPUB n'est pas gardé.
- Instance injoignable : le service est mis en pause (`source.broken`, raison `unreachable`) après
  plusieurs cycles ; il reprend seul au premier cycle qui passe.

## Contrat REST épinglé (version 1.x)

Il n'existe pas de lncrawl réel en CI (Shelfhold n'en active aucun) : les tests tournent contre un
faux serveur synthétique fidèle à ce sous-ensemble, qui est celui que l'adaptateur appelle et
rien d'autre. La plage de versions annoncée est `1.x` (`GET /api/v1/status`, champ `version`).

| Route | Rôle |
|---|---|
| `GET /api/v1/status` | version et identifiant de l'installation (empreinte, réinstallation) |
| `GET /api/v1/library` | romans de votre compte (pages, `items` + `next`) |
| `GET /api/v1/novels/{id}` | titre, auteurs, langue, résumé |
| `GET /api/v1/novels/{id}/chapters` | chapitres : `id`, `order`, `title`, `volume`, `closed`, `team` |
| `POST /api/v1/jobs` | crée un EPUB pour `novel`, `first`, `last` |
| `GET /api/v1/jobs/{id}` | `state` (`queued`, `running`, `done`, `failed`, `cancelled`), plage et chapitres couverts, `artifact` |
| `POST /api/v1/jobs/{id}/cancel` | annule |
| `GET /api/v1/artifacts/{id}/file` | l'EPUB |

La clé voyage dans l'en-tête `X-API-Key`. Aucune route ne liste ni ne choisit un crawler.
