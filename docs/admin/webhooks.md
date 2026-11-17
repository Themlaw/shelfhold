# Ce qui sort de l'instance : notifications et webhooks

Shelfhold parle vers l'extérieur pour deux choses : **prévenir** (l'administrateur d'abord, et
depuis M4 chaque adulte dans son coin) et, plus tard, **prévenir vos automatismes** (webhooks
signés). Cette page couvre les sorties propres à M4 ; les canaux de l'administrateur (Apprise,
résumé quotidien, schémas autorisés, garde SSRF) sont dans [notifications.md](notifications.md) —
rien n'est répété ici.

## La destination de notification de chaque adulte (M4)

Chaque profil adulte peut avoir **sa** destination de notification : une URL Apprise à lui, où
vont les titres de ses avis (au plus 20 par message, le reste compté). C'est toujours la sienne :

- `GET /api/v1/me/notification-destination` dit si une destination est réglée (rien d'autre :
  l'URL n'est jamais relue) ;
- `PUT /api/v1/me/notification-destination` la règle — une **action réservée** : session fraîche
  d'un adulte, jamais une clé d'API, jamais un jeton, jamais un enfant ;
- `DELETE` l'efface.

L'URL est **scellée** comme tous les secrets, et le schéma doit être dans la liste fermée
(`ntfy`, `ntfys`, `gotify`, `gotifys`, `matrix`, `matrixs`, `discord`, `slack`, `tgram`,
`pover`) : les webhooks génériques, le courrier et les schémas `http(s)` sont refusés, et un
serveur auto-hébergé passe la même garde d'adresse que les canaux de l'administrateur, à la
saisie **et avant chaque envoi**. Un message vers la destination d'un adulte ne passe **jamais**
sur le canal de l'instance (qui est lu par tous, enfants compris : il ne porte jamais de titre) ;
un échec est retenté, jamais rabattu sur le canal de l'instance. Un enfant n'a pas de
destination, même si une ligne existait.

**Il n'y a pas encore d'écran** pour la régler : l'API est là, le réglage se fait par elle.

## Le rythme : `notify_mode`

Dans **Réglages, Notifications**, chaque profil choisit `digest` (le résumé quotidien) ou
`immediate` (tout de suite), une période calme et son fuseau. Un enfant n'a jamais de
notification immédiate, y compris pour l'arrivée de sa propre demande.

## Les webhooks signés : pas encore disponible

Le cahier des charges prévoit une **outbox persistée** et des **webhooks signés** (HMAC-SHA256,
`schema_version`, catalogue `x-webhooks`), filtrés par la même table de visibilité que le reste :
le titre d'une demande privée n'en sort jamais. **Rien de cela n'est livré** : pas de catalogue de
webhooks, pas d'outbox, pas de signature. Aucun réglage ne s'y rapporte ; toute documentation qui
décrirait un webhook sortant aujourd'hui serait fausse. Les événements restent lisibles par
l'administrateur (page Activité, [journaux.md](journaux.md)) et par le flux d'événements de
l'interface.

En attendant, pour brancher un automatisme, utilisez le flux d'événements de l'interface
(`GET /api/v1/events`) ou la lecture des journaux ([journaux.md](journaux.md),
[diagnostic.md](diagnostic.md)) — pas un webhook.
