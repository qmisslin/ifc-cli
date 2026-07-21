# Spécification d’un programme de manipulation IFC simplifiée

## 1. Objectif

Le projet consiste à développer un programme console compilé permettant de charger, consulter et modifier de manière simplifiée un fichier IFC.

Le programme utilise la bibliothèque **IfcOpenShell en C++**, déjà compilée et disponible dans l’environnement du projet.

Le programme doit pouvoir être exécuté :

* directement depuis un terminal ;
* comme processus enfant depuis une autre application, par exemple un serveur C#.

Toutes les communications avec le programme utilisent un protocole **JSONL** : une requête JSON par ligne en entrée et une ou plusieurs réponses JSON par ligne en sortie.

Le programme n’a pas vocation à devenir un éditeur IFC complet. Il fournit une abstraction simplifiée permettant :

* la visualisation de la hiérarchie IFC ;
* la consultation des propriétés ;
* la modification des placements et propriétés compatibles ;
* la création d’éléments génériques ;
* l’ajout de matière à un élément existant ;
* la soustraction de matière à un élément existant ;
* la création et l’affectation de matériaux simples ;
* la tessellation des éléments pour leur visualisation.

Les géométries créées par l’API sont limitées à des profils polygonaux 2D extrudés.

Les matériaux créés par l’API sont limités à des matériaux simples possédant une catégorie unique et une apparence visuelle simple.

---

## 2. Principes de conservation du fichier IFC

Le fichier IFC chargé reste la source de vérité du programme.

Les représentations simplifiées exposées par l’API ne doivent pas être utilisées pour reconstruire entièrement le fichier lors de la sauvegarde.

Le programme modifie directement le modèle IFC chargé en mémoire.

Il doit préserver les entités, attributs, relations, propriétés, représentations et matériaux qu’il ne prend pas directement en charge, sauf lorsqu’ils doivent être supprimés en conséquence d’une commande de suppression explicite.

Les index internes de transforms, géométries et matériaux sont des vues simplifiées du modèle IFC.

Le programme doit pouvoir charger tout fichier IFC-SPF syntaxiquement valide dont le schéma est pris en charge par la compilation actuelle d’IfcOpenShell.

Les fichiers invalides, corrompus, utilisant un schéma non compilé ou contenant des constructions non prises en charge par le moteur géométrique peuvent être refusés avec une erreur explicite.

La sauvegarde doit sérialiser directement le modèle IFC actuellement chargé et modifié en mémoire.

---

## 3. Modèle chargé en mémoire

Lors du chargement d’un fichier IFC, le programme construit trois index principaux :

* les transforms ;
* les géométries ;
* les matériaux.

Ces index référencent directement ou indirectement les entités du fichier IFC chargé.

### 3.1 Transforms

Les transforms représentent les entités participant à la structure spatiale, au placement ou à la représentation de la scène.

### 3.2 Géométries

Les géométries représentent :

* les éléments géométriques existants du fichier IFC ;
* les opérations additives créées par l’API ;
* les opérations soustractives créées par l’API.

### 3.3 Matériaux

Les matériaux représentent :

* les matériaux existants du fichier IFC ;
* les structures de matériaux complexes existantes, exposées en lecture ;
* les matériaux simples créés par l’API.

### 3.4 Session unique

Un seul fichier IFC peut être chargé à la fois.

Le chargement d’un nouveau fichier détruit la session précédente et invalide tous ses identifiants.

---

## 4. Identifiants

Chaque objet exposé par l’API possède un identifiant opaque de session.

Exemples :

```text
transform:42
geometry:17
material:8
```

Cet identifiant doit être unique pendant toute la durée de la session IFC chargée.

Le client ne doit jamais générer lui-même un identifiant de session.

Le client doit uniquement réutiliser les identifiants retournés par l’API.

L’identifiant de session ne doit pas dépendre exclusivement de l’identifiant STEP de l’entité.

Lorsque l’information existe, les réponses exposent également :

* `ifcEntityId` : identifiant STEP courant de l’entité ;
* `globalId` : identifiant IFC global pour les entités héritant de `IfcRoot`.

Les identifiants STEP peuvent changer après une sauvegarde et un rechargement.

Les matériaux IFC et certains objets géométriques ne possèdent pas de `GlobalId`. Dans ce cas, la valeur retournée est `null`.

Après un nouvel appel à `load`, tous les identifiants de session précédents deviennent invalides.

---

## 5. Protocole JSONL

### 5.1 Requête

Chaque requête tient sur une seule ligne JSON.

La structure exacte d’une requête est :

```json
{
  "id": "request-42",
  "command": "transformGet",
  "params": {
    "id": "transform:12"
  }
}
```

Les trois champs de premier niveau sont obligatoires.

| Champ     | Type            | Description                                      |
| --------- | --------------- | ------------------------------------------------ |
| `id`      | chaîne non vide | Identifiant de corrélation choisi par le client. |
| `command` | chaîne non vide | Nom exact de la commande.                        |
| `params`  | objet JSON      | Paramètres exacts de la commande.                |

Le champ `id` identifie uniquement la requête JSONL. Il est indépendant des identifiants de session IFC.

### 5.2 Réponse en succès

Chaque réponse en succès utilise exactement la structure suivante :

```json
{
  "id": "request-42",
  "ok": true,
  "result": {}
}
```

| Champ    | Type             | Description                               |
| -------- | ---------------- | ----------------------------------------- |
| `id`     | chaîne ou `null` | Identifiant de la requête correspondante. |
| `ok`     | booléen          | Toujours `true`.                          |
| `result` | valeur JSON      | Résultat spécifique à la commande.        |

Aucun champ `error` ne doit être présent dans une réponse en succès.

### 5.3 Réponse en erreur

Chaque réponse en erreur utilise exactement la structure suivante :

```json
{
  "id": "request-42",
  "ok": false,
  "error": {
    "code": "ENTITY_NOT_FOUND",
    "message": "Transform transform:12 does not exist",
    "details": {}
  }
}
```

| Champ           | Type             | Description                               |
| --------------- | ---------------- | ----------------------------------------- |
| `id`            | chaîne ou `null` | Identifiant de la requête correspondante. |
| `ok`            | booléen          | Toujours `false`.                         |
| `error.code`    | chaîne           | Code d’erreur normalisé.                  |
| `error.message` | chaîne           | Description lisible de l’erreur.          |
| `error.details` | objet JSON       | Informations supplémentaires éventuelles. |

Aucun champ `result` ne doit être présent dans une réponse en erreur.

### 5.4 Contraintes d’entrée et de sortie

Les règles suivantes sont obligatoires :

* `stdin` est utilisé exclusivement pour recevoir les requêtes JSONL ;
* `stdout` est utilisé exclusivement pour écrire les réponses JSONL ;
* `stderr` est utilisé pour les logs techniques ;
* chaque requête occupe exactement une ligne ;
* chaque réponse occupe exactement une ligne ;
* chaque réponse est immédiatement flushée ;
* l’encodage utilisé est UTF-8 ;
* aucune sortie parasite ne doit être écrite sur `stdout` ;
* chaque commande retourne une réponse, y compris en cas d’échec ;
* une erreur sur une ligne ne doit pas interrompre la lecture des lignes suivantes ;
* seule une commande `exit` valide termine normalement le processus.

Une ligne entièrement vide peut être ignorée.

Une ligne contenant uniquement des espaces peut également être ignorée.

Toute autre ligne doit être interprétée comme une requête JSON complète.

### 5.5 Caractère normatif et strict du protocole

La structure des requêtes, des réponses et des paramètres décrite dans cette spécification est normative.

L’implémentation doit suivre exactement :

* les noms de commandes ;
* la casse des noms de commandes ;
* les noms de champs ;
* les types JSON ;
* les structures JSON ;
* les valeurs d’énumération ;
* les règles concernant les champs obligatoires et facultatifs.

Aucun format alternatif ne doit être accepté.

Le programme ne doit notamment pas accepter :

* des commandes textuelles comme `load(path)` ;
* des paramètres positionnels sous forme de tableau ;
* un champ `args` à la place de `params` ;
* un champ `action`, `method` ou `route` à la place de `command` ;
* un alias de commande ;
* une commande avec une casse différente ;
* plusieurs structures JSON différentes pour une même commande ;
* des champs supplémentaires non définis ;
* des conversions implicites de types ;
* des valeurs par défaut pour un champ obligatoire absent.

Une commande sans paramètre utilise obligatoirement un objet `params` vide :

```json
{
  "id": "request-42",
  "command": "transformGetAll",
  "params": {}
}
```

Les structures suivantes sont invalides :

```json
{
  "id": "request-42",
  "command": "transformGetAll"
}
```

```json
{
  "id": "request-42",
  "command": "transformGetAll",
  "params": null
}
```

```json
{
  "id": "request-42",
  "method": "transformGetAll",
  "params": {}
}
```

```json
{
  "id": "request-42",
  "command": "TransformGetAll",
  "params": {}
}
```

Les noms suivants ne sont pas équivalents :

```text
transformGet
TransformGet
transform_get
transformget
transform-get
```

Seul `transformGet` est valide.

### 5.6 Validation stricte des champs

Les champs supplémentaires sont interdits.

Par exemple, la requête suivante est invalide si `debug` n’est pas défini dans la commande :

```json
{
  "id": "request-42",
  "command": "transformGet",
  "params": {
    "id": "transform:12",
    "debug": true
  }
}
```

Elle doit retourner `INVALID_PARAMETERS`.

Les valeurs JSON ne doivent jamais être automatiquement converties.

Exemples :

* `"42"` est invalide lorsqu’un nombre est attendu ;
* `42` est invalide lorsqu’une chaîne est attendue ;
* `1` est invalide lorsqu’un booléen est attendu ;
* `"true"` est invalide lorsqu’un booléen est attendu ;
* `""` est invalide lorsqu’une chaîne non vide est attendue ;
* `null` est invalide sauf lorsque cette valeur est explicitement autorisée.

Les nombres doivent être finis.

Les valeurs suivantes sont interdites :

```text
NaN
Infinity
-Infinity
```

Le programme ne doit jamais :

* corriger automatiquement une requête ;
* compléter automatiquement un paramètre manquant ;
* ignorer silencieusement un champ inconnu ;
* normaliser un nom de commande ;
* convertir implicitement une valeur ;
* interpréter plusieurs variantes d’un même message.

### 5.7 Requête JSON invalide

Lorsqu’une ligne n’est pas un document JSON valide, la réponse utilise :

```json
{
  "id": null,
  "ok": false,
  "error": {
    "code": "INVALID_JSON",
    "message": "Invalid JSONL request",
    "details": {}
  }
}
```

Lorsque la racine JSON est valide mais que le champ `id` est absent ou invalide, la réponse utilise également `"id": null`.

### 5.8 Nombre de réponses

Toutes les commandes, à l’exception de `transformTessellate`, produisent exactement une ligne de réponse.

`transformTessellate` peut produire plusieurs lignes.

Chaque ligne de tessellation utilise l’enveloppe de succès standard :

```json
{
  "id": "request-42",
  "ok": true,
  "result": {
    "event": "mesh.begin"
  }
}
```

Si la tessellation échoue avant l’émission de `mesh.begin`, une réponse d’erreur standard est retournée.

Après l’émission de `mesh.begin`, la commande doit soit terminer par `mesh.end`, soit produire une réponse d’erreur portant le même identifiant de requête.

---

## 6. Transforms

### 6.1 Définition

Un transform représente une entité IFC participant à la structure ou à la représentation spatiale du modèle.

Un transform peut notamment correspondre à :

* un projet ;
* un site ;
* un bâtiment ;
* un étage ;
* un espace ;
* un élément constructif ;
* un élément générique ;
* un élément possédant une géométrie ;
* une entité participant à une hiérarchie gérée par l’API.

Un transform peut posséder plusieurs relations parentes indépendantes :

* parent spatial ;
* parent de placement ;
* parent de décomposition.

Ces relations ne doivent pas être fusionnées dans un unique `parentId`.

### 6.2 Parents

Les parents sont exposés avec la structure suivante :

```json
{
  "spatial": "transform:10",
  "placement": "transform:10",
  "decomposition": null
}
```

| Champ           | Type                               | Description                              |
| --------------- | ---------------------------------- | ---------------------------------------- |
| `spatial`       | identifiant de transform ou `null` | Parent de containment spatial.           |
| `placement`     | identifiant de transform ou `null` | Parent du placement relatif.             |
| `decomposition` | identifiant de transform ou `null` | Parent de décomposition ou d’agrégation. |

Ces relations sont indépendantes.

Une même entité peut être utilisée dans plusieurs champs.

Lorsqu’un objet `parents` est fourni dans une commande de création ou de mise à jour, les trois champs sont obligatoires.

Une valeur `null` signifie qu’aucune relation de ce type ne doit exister après l’opération.

### 6.3 Placement

Les données de placement retournées sont :

```json
{
  "local": [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0
  ],
  "world": [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0
  ],
  "editable": true,
  "sourceType": "IfcLocalPlacement"
}
```

La matrice `world` est calculée à partir de la matrice locale et de la hiérarchie de placement.

Les matrices `local` et `world` ne sont jamais indépendamment modifiables.

Lors d’une création ou d’une mise à jour, le client fournit une seule matrice et indique son espace :

```json
{
  "space": "parent",
  "matrix": [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0
  ]
}
```

Les valeurs possibles de `space` sont exclusivement :

* `parent` ;
* `world`.

Les valeurs possibles de `sourceType` peuvent notamment être :

* `IfcLocalPlacement` ;
* `IfcGridPlacement` ;
* `IfcLinearPlacement` ;
* `virtual`.

`sourceType` est une valeur de sortie en lecture seule.

### 6.4 Matrices

Les matrices sont représentées par un tableau contenant exactement 16 nombres finis.

L’ordre utilisé est **row-major**.

Les points sont considérés comme des vecteurs colonnes homogènes :

```text
p' = M × p
```

Les composantes de translation se trouvent aux indices :

```text
3
7
11
```

La matrice suivante représente une translation de `(2, 1, 0)` :

```json
[
  1.0, 0.0, 0.0, 2.0,
  0.0, 1.0, 0.0, 1.0,
  0.0, 0.0, 1.0, 0.0,
  0.0, 0.0, 0.0, 1.0
]
```

Aucune représentation alternative sous forme de quatre tableaux imbriqués n’est acceptée.

### 6.5 Transform virtuel

Lorsqu’une entité appartient à la hiérarchie mais ne possède aucun placement, le programme expose un transform virtuel.

Un transform virtuel utilise une matrice locale identité.

Il peut être éditable uniquement si l’entité IFC sous-jacente peut recevoir un `ObjectPlacement`.

Lorsqu’un transform virtuel éditable est modifié, le programme peut matérialiser un `IfcLocalPlacement`.

### 6.6 Transforms créés par l’API

Les transforms créés dynamiquement utilisent exclusivement la classe :

```text
IfcBuildingElementProxy
```

La classe IFC est fixée lors de la création.

Elle ne peut pas être modifiée par cette API.

Les transforms créés par l’API peuvent recevoir :

* un nom ;
* un placement ;
* un parent spatial ;
* un parent de placement ;
* un parent de décomposition ;
* des propriétés ;
* des géométries extrudées ;
* des matériaux simples.

### 6.7 Structure retournée

La structure complète retournée par `transformGet` est :

```json
{
  "id": "transform:42",
  "ifcEntityId": 123,
  "globalId": "2XQ$n5SLP5MBLyL442paFx",
  "name": "Wall 001",
  "parents": {
    "spatial": "transform:10",
    "placement": "transform:10",
    "decomposition": null
  },
  "transforms": {
    "local": [
      1.0, 0.0, 0.0, 0.0,
      0.0, 1.0, 0.0, 0.0,
      0.0, 0.0, 1.0, 0.0,
      0.0, 0.0, 0.0, 1.0
    ],
    "world": [
      1.0, 0.0, 0.0, 0.0,
      0.0, 1.0, 0.0, 0.0,
      0.0, 0.0, 1.0, 0.0,
      0.0, 0.0, 0.0, 1.0
    ],
    "editable": true,
    "sourceType": "IfcLocalPlacement"
  },
  "properties": [],
  "geometries": [
    "geometry:17"
  ],
  "materials": [
    "material:8"
  ],
  "definition": {
    "ifcClass": "IfcWall",
    "editableClass": false,
    "attributes": {},
    "references": {}
  }
}
```

Le champ `definition` fournit un accès en lecture aux données IFC non directement prises en charge par l’API.

Il ne doit pas contenir une sérialisation récursive complète du graphe IFC.

Les références vers d’autres entités exposées doivent utiliser des identifiants de session.

---

## 7. Propriétés

### 7.1 Structure

Les propriétés doivent identifier explicitement leur property set.

Une propriété utilise la structure suivante :

```json
{
  "propertySet": "Pset_Custom",
  "name": "Reference",
  "valueType": "IfcLabel",
  "value": "ABC-123",
  "unit": null,
  "source": "occurrence"
}
```

| Champ         | Type                              | Description                  |
| ------------- | --------------------------------- | ---------------------------- |
| `propertySet` | chaîne non vide                   | Nom du property set.         |
| `name`        | chaîne non vide                   | Nom de la propriété.         |
| `valueType`   | chaîne non vide                   | Type IFC exact de la valeur. |
| `value`       | chaîne, nombre, booléen ou `null` | Valeur de la propriété.      |
| `unit`        | chaîne ou `null`                  | Unité de la propriété.       |
| `source`      | chaîne                            | Origine de la propriété.     |

Les valeurs possibles de `source` retournées en lecture peuvent notamment être :

* `occurrence` ;
* `type` ;
* `material` ;
* `quantity`.

Les propriétés modifiables d’un transform doivent utiliser :

```text
occurrence
```

Les propriétés modifiables d’un matériau doivent utiliser :

```text
material
```

Les attributs directs de l’entité IFC, tels que `Name`, ne sont pas exposés comme des propriétés de property set.

Les propriétés de type liste, table, valeur bornée ou structure complexe sont exposées dans `definition` lorsqu’elles ne sont pas prises en charge par l’API simplifiée.

### 7.2 Remplacement complet

Lors d’une création ou d’une mise à jour, le tableau `properties` représente l’intégralité des propriétés éditables de l’objet.

Le tableau fourni remplace entièrement l’état précédent.

Exemple :

```json
{
  "properties": [
    {
      "propertySet": "Pset_Custom",
      "name": "Reference",
      "valueType": "IfcLabel",
      "value": "ABC-123",
      "unit": null,
      "source": "occurrence"
    },
    {
      "propertySet": "Pset_Custom",
      "name": "Enabled",
      "valueType": "IfcBoolean",
      "value": true,
      "unit": null,
      "source": "occurrence"
    }
  ]
}
```

Lors d’une mise à jour :

* une propriété présente dans le nouveau tableau est créée ou mise à jour ;
* une propriété éditable précédemment présente mais absente du nouveau tableau est supprimée du graphe IFC ;
* un tableau vide supprime toutes les propriétés éditables de l’objet ;
* un champ `properties` absent signifie que les propriétés ne sont pas modifiées.

La suppression d’une propriété doit nettoyer les entités devenues inutilisées et exclusivement associées à cette propriété.

Ce nettoyage peut notamment concerner :

* les instances `IfcPropertySingleValue` ;
* les relations de propriétés correspondantes ;
* les property sets devenus vides ;
* les objets auxiliaires exclusivement possédés.

Les propriétés non éditables ou non prises en charge restent accessibles dans `definition`.

Elles ne doivent pas être supprimées lors du remplacement du tableau `properties`.

### 7.3 Unicité

Dans un même tableau `properties`, le couple suivant doit être unique :

```text
propertySet + name
```

Une requête contenant deux propriétés avec le même `propertySet` et le même `name` doit retourner `INVALID_PARAMETERS`.

---

## 8. Géométries

### 8.1 Géométries natives

Les représentations géométriques existantes du fichier IFC sont indexées afin de permettre :

* leur inspection ;
* leur association à un transform ;
* leur tessellation ;
* leur identification ;
* leur suppression explicite.

Une géométrie IFC native peut être représentée par :

```json
{
  "id": "geometry:17",
  "ifcEntityId": 832,
  "globalId": null,
  "source": "ifc",
  "editable": false,
  "ifcClass": "IfcMappedItem",
  "operation": "BASE",
  "parentId": "transform:42",
  "definition": {}
}
```

L’API ne garantit pas la modification paramétrique d’une géométrie native arbitraire.

Le champ `editable` indique si `geometryUpdate` est autorisé.

Une géométrie native peut néanmoins être supprimée avec `geometryDelete`.

### 8.2 Géométries créées par l’API

Les géométries créées par l’API sont des opérations géométriques simples basées sur l’extrusion d’un profil polygonal.

Deux opérations sont prises en charge :

* `ADD` : ajout de matière ;
* `SUBTRACT` : soustraction de matière.

Chaque opération créée par l’API est associée à un transform cible.

### 8.3 Ajout de matière

Une opération `ADD` est représentée dans le modèle IFC par :

```text
IfcProjectionElement
IfcRelProjectsElement
IfcExtrudedAreaSolid
```

L’élément projeté possède sa propre représentation et son propre placement.

La représentation géométrique originale du transform cible n’est pas modifiée.

### 8.4 Soustraction de matière

Une opération `SUBTRACT` est représentée dans le modèle IFC par :

```text
IfcOpeningElement
IfcRelVoidsElement
IfcExtrudedAreaSolid
```

L’ouverture possède sa propre représentation et son propre placement.

La représentation géométrique originale du transform cible n’est pas modifiée.

### 8.5 Restrictions

Une opération géométrique ne peut être créée que si le transform cible est compatible avec la relation IFC correspondante.

L’API retourne une erreur si :

* le transform ne peut pas recevoir de projection ;
* le transform ne peut pas recevoir d’ouverture ;
* le schéma IFC ne prend pas en charge la relation nécessaire ;
* le profil polygonal est invalide ;
* le placement demandé ne peut pas être représenté ;
* la profondeur d’extrusion est invalide.

Les opérations créées par l’API sont éditables.

Les géométries natives existantes sont en lecture seule pour `geometryUpdate`, sauf prise en charge explicite ultérieure.

### 8.6 Profil polygonal

Le champ `profile` contient directement la liste ordonnée des points 2D du profil.

Exemple :

```json
{
  "profile": [
    [0.0, 0.0],
    [2.0, 0.0],
    [2.0, 1.0],
    [0.0, 1.0]
  ]
}
```

Le profil est implicitement fermé entre le dernier point et le premier point.

Le premier point ne doit pas être répété à la fin du tableau.

Chaque point doit être un tableau contenant exactement deux nombres finis.

Le profil doit :

* contenir au minimum trois points distincts ;
* ne pas contenir d’auto-intersection ;
* ne pas contenir de segment de longueur nulle ;
* ne pas contenir de points consécutifs identiques ;
* définir une surface non nulle ;
* utiliser les unités du modèle IFC ;
* être défini dans le plan local XY de la géométrie.

L’extrusion est toujours effectuée selon l’axe local positif Z de la géométrie.

La position et la direction de l’extrusion sont intégralement déterminées par le transform de la géométrie.

La profondeur est définie par le champ `depth`.

Elle doit être un nombre fini strictement positif.

Un carré ou un rectangle est représenté par quatre points.

Un cercle est représenté par une approximation polygonale dont le nombre de points est choisi par l’application cliente.

L’API ne prend pas directement en charge :

* les arcs analytiques ;
* les cercles analytiques ;
* les courbes splines ;
* les profils contenant des trous ;
* les profils composés de plusieurs contours.

### 8.7 Structure d’une géométrie éditable

Une géométrie éditable retournée par l’API utilise la structure suivante :

```json
{
  "id": "geometry:101",
  "ifcEntityId": 901,
  "globalId": "0abc...",
  "source": "editor",
  "editable": true,
  "ifcClass": "IfcOpeningElement",
  "parentId": "transform:42",
  "name": "Opening 01",
  "operation": "SUBTRACT",
  "transforms": {
    "local": [
      1.0, 0.0, 0.0, 0.0,
      0.0, 1.0, 0.0, 0.0,
      0.0, 0.0, 1.0, 0.0,
      0.0, 0.0, 0.0, 1.0
    ],
    "world": [
      1.0, 0.0, 0.0, 0.0,
      0.0, 1.0, 0.0, 0.0,
      0.0, 0.0, 1.0, 0.0,
      0.0, 0.0, 0.0, 1.0
    ],
    "editable": true,
    "sourceType": "IfcLocalPlacement"
  },
  "profile": [
    [0.0, 0.0],
    [1.2, 0.0],
    [1.2, 2.1],
    [0.0, 2.1]
  ],
  "depth": 0.3,
  "materials": [
    "material:8"
  ],
  "definition": {}
}
```

---

## 9. Matériaux

### 9.1 Matériaux natifs

Les matériaux existants sont indexés, qu’ils soient simples ou complexes.

Les structures complexes peuvent notamment inclure :

* des ensembles de couches ;
* des listes de matériaux ;
* des ensembles de constituants ;
* des profils de matériaux ;
* des styles de représentation.

Ces structures sont accessibles en lecture via le champ `definition`.

Les matériaux complexes existants ne sont pas nécessairement éditables par l’API simplifiée.

### 9.2 Matériaux simples créés par l’API

Un matériau créé par l’API comprend :

* un nom ;
* une catégorie ;
* des propriétés ;
* une apparence visuelle simple.

Structure visuelle :

```json
{
  "color": [0.6, 0.6, 0.6],
  "opacity": 1.0,
  "metallic": 0.0,
  "roughness": 0.8
}
```

Les composantes de couleur sont comprises entre `0.0` et `1.0`.

Les champs suivants sont également compris entre `0.0` et `1.0` :

* `opacity` ;
* `metallic` ;
* `roughness`.

Tous les nombres doivent être finis.

La représentation IFC exacte de `metallic` et `roughness` dépend du schéma chargé.

Le programme peut appliquer une conversion ou une dégradation contrôlée lorsque ces propriétés ne sont pas directement disponibles dans le schéma.

### 9.3 Matériau physique et apparence visuelle

Le programme doit gérer en interne la distinction entre :

* le matériau sémantique IFC ;
* le style visuel utilisé pour le rendu.

L’API expose ces deux concepts sous une abstraction unique de matériau simple.

### 9.4 Structure retournée

La structure complète retournée par `materialGet` est :

```json
{
  "id": "material:8",
  "ifcEntityId": 346,
  "globalId": null,
  "name": "Concrete",
  "category": "Concrete",
  "editable": true,
  "visual": {
    "color": [0.6, 0.6, 0.6],
    "opacity": 1.0,
    "metallic": 0.0,
    "roughness": 0.8
  },
  "properties": [],
  "definition": {
    "ifcClass": "IfcMaterial",
    "attributes": {},
    "references": {}
  }
}
```

Le champ `definition` contient les informations non éditables ou non prises en charge.

---

## 10. Suppression

### 10.1 Principes généraux

Les commandes de suppression sont destructives.

Elles ne demandent aucune confirmation.

L’API ne retourne pas d’avertissement lorsqu’un élément supprimé possède des enfants ou est référencé par d’autres entités.

La responsabilité d’afficher un avertissement ou une confirmation à l’utilisateur final appartient à l’application cliente.

Après une suppression, le graphe IFC doit rester structurellement valide.

Il ne doit contenir aucune référence vers une entité supprimée.

Lorsqu’un attribut obligatoire référence une entité supprimée, l’entité ou la relation contenant cet attribut doit également être supprimée.

Les suppressions doivent utiliser un ensemble interne d’entités déjà visitées afin d’éviter :

* les suppressions multiples ;
* les boucles ;
* les récursions infinies ;
* le traitement répété d’une même entité accessible par plusieurs relations.

### 10.2 Suppression d’un transform

La suppression d’un transform entraîne :

* la suppression du transform IFC correspondant ;
* la suppression récursive de ses transforms enfants ;
* la suppression de ses géométries ;
* la suppression de ses opérations additives ;
* la suppression de ses opérations soustractives ;
* la suppression de ses relations spatiales ;
* la suppression de ses relations de placement ;
* la suppression de ses relations de décomposition ;
* la suppression de ses affectations de matériaux ;
* la suppression de toutes les relations référençant le transform supprimé ;
* le nettoyage des entités devenues orphelines et exclusivement possédées.

Les enfants concernés sont ceux liés au transform par une relation hiérarchique gérée par l’API, notamment :

* le containment spatial ;
* la décomposition ;
* le placement relatif.

Une même entité ne doit être supprimée qu’une seule fois, même si elle est accessible par plusieurs relations.

Les objets partagés qui ne font pas partie de la hiérarchie supprimée ne sont pas automatiquement supprimés.

Leurs références vers les entités supprimées sont retirées.

### 10.3 Suppression d’une géométrie

La suppression d’une géométrie entraîne :

* la suppression de l’objet géométrique indexé ;
* la suppression de ses relations avec son transform parent ;
* la suppression de ses profils exclusivement possédés ;
* la suppression de ses placements exclusivement possédés ;
* la suppression de ses représentations exclusivement possédées ;
* la suppression de ses affectations de matériaux ;
* la suppression de toutes les références vers cette géométrie.

Pour une opération `ADD`, le programme supprime notamment :

```text
IfcRelProjectsElement
IfcProjectionElement
IfcExtrudedAreaSolid
```

Pour une opération `SUBTRACT`, le programme supprime notamment :

```text
IfcRelVoidsElement
IfcOpeningElement
IfcExtrudedAreaSolid
```

Si une géométrie native est supprimée, le programme supprime également son rattachement à la représentation du produit.

Si une représentation ne contient plus aucun élément après cette suppression, la représentation vide et les objets devenus orphelins doivent être nettoyés.

### 10.4 Suppression d’un matériau

La suppression d’un matériau est toujours autorisée.

Elle entraîne :

* la suppression de toutes ses affectations aux transforms ;
* la suppression de toutes ses affectations aux géométries ;
* la suppression des relations de matériau correspondantes ;
* la suppression de sa représentation visuelle ;
* la suppression des styles exclusivement associés ;
* la suppression de ses propriétés ;
* la suppression de toutes les références vers le matériau ;
* la suppression de l’entité de matériau elle-même.

Les transforms et géométries utilisant le matériau ne sont pas supprimés.

Ils deviennent simplement sans affectation pour ce matériau.

Les styles ou représentations partagés avec d’autres matériaux ne doivent pas être supprimés.

Seules leurs références vers le matériau supprimé sont retirées.

---

## 11. Types JSON communs

Les structures définies dans cette section sont normatives.

Aucune structure alternative n’est acceptée.

### 11.1 `SessionId`

Un identifiant de session est une chaîne non vide retournée par l’API.

Les préfixes autorisés sont :

```text
transform:
geometry:
material:
```

La commande détermine le préfixe attendu.

Par exemple, `transformGet` n’accepte qu’un identifiant commençant par `transform:`.

### 11.2 `Matrix4`

Une matrice contient exactement 16 nombres finis.

```json
[
  1.0, 0.0, 0.0, 0.0,
  0.0, 1.0, 0.0, 0.0,
  0.0, 0.0, 1.0, 0.0,
  0.0, 0.0, 0.0, 1.0
]
```

### 11.3 `TransformInput`

```json
{
  "space": "parent",
  "matrix": [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0
  ]
}
```

| Champ    | Type      | Valeurs autorisées          |
| -------- | --------- | --------------------------- |
| `space`  | chaîne    | `parent` ou `world`         |
| `matrix` | `Matrix4` | Exactement 16 nombres finis |

Les deux champs sont obligatoires.

Aucun autre champ n’est autorisé.

### 11.4 `ParentsInput`

```json
{
  "spatial": "transform:10",
  "placement": "transform:10",
  "decomposition": null
}
```

Chaque champ accepte :

* un identifiant de transform ;
* `null`.

Les trois champs sont obligatoires lorsqu’un objet `parents` est fourni.

### 11.5 `PropertyInput`

```json
{
  "propertySet": "Pset_Custom",
  "name": "Reference",
  "valueType": "IfcLabel",
  "value": "ABC-123",
  "unit": null,
  "source": "occurrence"
}
```

Les six champs sont obligatoires.

Aucun autre champ n’est autorisé.

### 11.6 `ProfileInput`

```json
[
  [0.0, 0.0],
  [1.2, 0.0],
  [1.2, 2.1],
  [0.0, 2.1]
]
```

Le tableau contient au minimum trois points.

Chaque point contient exactement deux nombres finis.

### 11.7 `VisualInput`

```json
{
  "color": [0.6, 0.6, 0.6],
  "opacity": 1.0,
  "metallic": 0.0,
  "roughness": 0.8
}
```

Les quatre champs sont obligatoires.

Le champ `color` contient exactement trois nombres.

Chaque valeur est comprise entre `0.0` et `1.0`.

### 11.8 Mise à jour partielle

Les commandes `transformUpdate`, `geometryUpdate` et `materialUpdate` utilisent une structure unique avec des champs facultatifs.

Un champ facultatif absent signifie que la valeur correspondante n’est pas modifiée.

Un champ présent doit respecter exactement son type complet.

Au moins un champ modifiable doit être présent.

Un objet vide ne constitue pas une mise à jour valide.

---

## 12. Commandes disponibles

Les noms de commandes définis dans cette section sont les seuls noms autorisés.

Les signatures textuelles servent uniquement à documenter les commandes.

Elles ne constituent pas un format d’entrée.

Le seul format d’entrée accepté est le JSONL défini dans la section 5.

### 12.1 Commandes générales

#### ROUTE API : `help`

Retourne la documentation intégrée du protocole et la liste des commandes.

Paramètres exacts :

```json
{}
```

Requête :

```json
{
  "id": "request-1",
  "command": "help",
  "params": {}
}
```

Résultat :

```json
{
  "documentation": "..."
}
```

La valeur `documentation` est une chaîne.

Cette commande ne nécessite pas qu’un modèle soit chargé.

#### ROUTE API : `getCapabilities`

Retourne les capacités de l’exécutable courant.

Paramètres exacts :

```json
{}
```

Résultat :

```json
{
  "protocolVersion": "1.0",
  "ifcOpenShellVersion": "...",
  "supportedSchemas": [
    "IFC2X3",
    "IFC4",
    "IFC4X3"
  ],
  "supportedProfiles": [
    "polygon"
  ],
  "supportedGeometryOperations": [
    "ADD",
    "SUBTRACT"
  ]
}
```

Les valeurs retournées doivent correspondre aux capacités réellement compilées.

Cette commande ne nécessite pas qu’un modèle soit chargé.

#### ROUTE API : `load`

Charge un fichier IFC et initialise une nouvelle session.

Paramètres exacts :

```json
{
  "path": "/path/to/model.ifc"
}
```

| Paramètre | Type            | Description                      |
| --------- | --------------- | -------------------------------- |
| `path`    | chaîne non vide | Chemin du fichier IFC à charger. |

Résultat :

```json
{
  "path": "/path/to/model.ifc",
  "schema": "IFC4",
  "transformCount": 100,
  "geometryCount": 80,
  "materialCount": 12
}
```

Le chargement invalide tous les identifiants de session précédents.

Si un modèle était déjà chargé, il est fermé avant la création de la nouvelle session.

#### ROUTE API : `save`

Sauvegarde le modèle IFC actuellement chargé.

Paramètres exacts :

```json
{
  "path": "/path/to/output.ifc"
}
```

| Paramètre | Type            | Description                     |
| --------- | --------------- | ------------------------------- |
| `path`    | chaîne non vide | Chemin du fichier IFC à écrire. |

Résultat :

```json
{
  "path": "/path/to/output.ifc"
}
```

La commande nécessite qu’un modèle soit chargé.

#### ROUTE API : `exit`

Ferme la session IFC et termine proprement le processus.

Paramètres exacts :

```json
{}
```

Résultat :

```json
{}
```

La réponse doit être écrite et flushée avant la fin du processus.

### 12.2 Commandes de transforms

#### ROUTE API : `transformGetAll`

Retourne la liste de tous les transforms indexés dans la session.

Paramètres exacts :

```json
{}
```

Résultat :

```json
[
  {
    "id": "transform:42",
    "ifcEntityId": 123,
    "globalId": "2XQ$n5SLP5MBLyL442paFx"
  }
]
```

`globalId` vaut `null` lorsqu’il n’existe pas.

#### ROUTE API : `transformGet`

Retourne la représentation complète d’un transform.

Paramètres exacts :

```json
{
  "id": "transform:42"
}
```

| Paramètre | Type                     | Description            |
| --------- | ------------------------ | ---------------------- |
| `id`      | identifiant de transform | Transform à retourner. |

Le résultat respecte la structure définie dans la section 6.7.

#### ROUTE API : `transformCreate`

Crée un `IfcBuildingElementProxy`.

Paramètres exacts :

```json
{
  "name": "Generic element",
  "parents": {
    "spatial": "transform:10",
    "placement": "transform:10",
    "decomposition": null
  },
  "transform": {
    "space": "parent",
    "matrix": [
      1.0, 0.0, 0.0, 0.0,
      0.0, 1.0, 0.0, 0.0,
      0.0, 0.0, 1.0, 0.0,
      0.0, 0.0, 0.0, 1.0
    ]
  },
  "properties": []
}
```

| Paramètre    | Type                       | Description                                          |
| ------------ | -------------------------- | ---------------------------------------------------- |
| `name`       | chaîne                     | Nom du transform.                                    |
| `parents`    | `ParentsInput`             | Relations parentes initiales.                        |
| `transform`  | `TransformInput`           | Placement initial.                                   |
| `properties` | tableau de `PropertyInput` | Ensemble complet des propriétés éditables initiales. |

Tous les champs sont obligatoires.

Le résultat est la structure complète du transform créé.

#### ROUTE API : `transformUpdate`

Met à jour un transform existant.

Paramètres :

```json
{
  "id": "transform:42",
  "name": "Updated element",
  "parents": {
    "spatial": "transform:11",
    "placement": "transform:11",
    "decomposition": null
  },
  "transform": {
    "space": "world",
    "matrix": [
      1.0, 0.0, 0.0, 2.0,
      0.0, 1.0, 0.0, 1.0,
      0.0, 0.0, 1.0, 0.0,
      0.0, 0.0, 0.0, 1.0
    ]
  },
  "properties": []
}
```

| Paramètre    | Type                       | Obligatoire | Description                                    |
| ------------ | -------------------------- | ----------: | ---------------------------------------------- |
| `id`         | identifiant de transform   |         oui | Transform à modifier.                          |
| `name`       | chaîne                     |         non | Nouveau nom.                                   |
| `parents`    | `ParentsInput`             |         non | Remplacement complet des relations parentes.   |
| `transform`  | `TransformInput`           |         non | Nouveau placement.                             |
| `properties` | tableau de `PropertyInput` |         non | Remplacement complet des propriétés éditables. |

Au moins un champ parmi `name`, `parents`, `transform` et `properties` doit être présent.

Le résultat est la structure complète du transform après modification.

#### ROUTE API : `transformDelete`

Supprime un transform, ses enfants et toutes les références correspondantes.

Paramètres exacts :

```json
{
  "id": "transform:42"
}
```

Résultat :

```json
{
  "id": "transform:42"
}
```

L’identifiant retourné n’est plus valide après la réponse.

#### ROUTE API : `transformTessellate`

Tesselle la géométrie finale d’un transform.

Paramètres exacts :

```json
{
  "id": "transform:42",
  "options": {
    "space": "world",
    "includeNormals": true,
    "includeMaterials": true,
    "includeChildren": false
  }
}
```

| Paramètre                  | Type                     | Description                      |
| -------------------------- | ------------------------ | -------------------------------- |
| `id`                       | identifiant de transform | Transform à tesseller.           |
| `options.space`            | chaîne                   | `local` ou `world`.              |
| `options.includeNormals`   | booléen                  | Inclut les normales.             |
| `options.includeMaterials` | booléen                  | Inclut les groupes de matériaux. |
| `options.includeChildren`  | booléen                  | Inclut les transforms enfants.   |

L’objet `options` et ses quatre champs sont obligatoires.

Les seuls événements autorisés sont :

```text
mesh.begin
mesh.vertices
mesh.indices
mesh.normals
mesh.materials
mesh.end
```

Début du maillage :

```json
{
  "id": "request-42",
  "ok": true,
  "result": {
    "event": "mesh.begin",
    "vertexCount": 1000,
    "indexCount": 3000
  }
}
```

Bloc de sommets :

```json
{
  "id": "request-42",
  "ok": true,
  "result": {
    "event": "mesh.vertices",
    "offset": 0,
    "values": [
      0.0,
      0.0,
      0.0
    ]
  }
}
```

Bloc d’indices :

```json
{
  "id": "request-42",
  "ok": true,
  "result": {
    "event": "mesh.indices",
    "offset": 0,
    "values": [
      0,
      1,
      2
    ]
  }
}
```

Bloc de normales :

```json
{
  "id": "request-42",
  "ok": true,
  "result": {
    "event": "mesh.normals",
    "offset": 0,
    "values": [
      0.0,
      0.0,
      1.0
    ]
  }
}
```

Bloc de matériaux :

```json
{
  "id": "request-42",
  "ok": true,
  "result": {
    "event": "mesh.materials",
    "groups": [
      {
        "materialId": "material:8",
        "indexOffset": 0,
        "indexCount": 300
      }
    ]
  }
}
```

Fin du maillage :

```json
{
  "id": "request-42",
  "ok": true,
  "result": {
    "event": "mesh.end"
  }
}
```

`mesh.normals` est omis lorsque `includeNormals` vaut `false`.

`mesh.materials` est omis lorsque `includeMaterials` vaut `false`.

### 12.3 Commandes de géométries

#### ROUTE API : `geometryGetAll`

Retourne toutes les géométries ou uniquement celles d’un transform donné.

Paramètres exacts :

```json
{
  "transformId": null
}
```

| Paramètre     | Type                               | Description           |
| ------------- | ---------------------------------- | --------------------- |
| `transformId` | identifiant de transform ou `null` | Filtre par transform. |

Le champ `transformId` est obligatoire.

La valeur `null` retourne toutes les géométries.

Résultat :

```json
[
  {
    "id": "geometry:17",
    "ifcEntityId": 832,
    "globalId": null,
    "parentId": "transform:42",
    "source": "ifc",
    "editable": false,
    "operation": "BASE"
  }
]
```

#### ROUTE API : `geometryGet`

Retourne une géométrie indexée.

Paramètres exacts :

```json
{
  "id": "geometry:17"
}
```

| Paramètre | Type                     | Description            |
| --------- | ------------------------ | ---------------------- |
| `id`      | identifiant de géométrie | Géométrie à retourner. |

#### ROUTE API : `geometryCreate`

Crée une opération géométrique polygonale extrudée.

Paramètres exacts :

```json
{
  "parentId": "transform:42",
  "name": "Opening 01",
  "transform": {
    "space": "parent",
    "matrix": [
      1.0, 0.0, 0.0, 0.0,
      0.0, 1.0, 0.0, 0.0,
      0.0, 0.0, 1.0, 0.0,
      0.0, 0.0, 0.0, 1.0
    ]
  },
  "profile": [
    [0.0, 0.0],
    [1.2, 0.0],
    [1.2, 2.1],
    [0.0, 2.1]
  ],
  "depth": 0.3,
  "operation": "SUBTRACT"
}
```

| Paramètre   | Type                       | Description                           |
| ----------- | -------------------------- | ------------------------------------- |
| `parentId`  | identifiant de transform   | Transform cible.                      |
| `name`      | chaîne                     | Nom de l’opération.                   |
| `transform` | `TransformInput`           | Placement de l’extrusion.             |
| `profile`   | `ProfileInput`             | Profil polygonal local XY.            |
| `depth`     | nombre strictement positif | Profondeur sur l’axe local positif Z. |
| `operation` | chaîne                     | `ADD` ou `SUBTRACT`.                  |

Tous les champs sont obligatoires.

Pour `ADD`, le programme crée :

```text
IfcProjectionElement
IfcRelProjectsElement
IfcExtrudedAreaSolid
```

Pour `SUBTRACT`, le programme crée :

```text
IfcOpeningElement
IfcRelVoidsElement
IfcExtrudedAreaSolid
```

Le résultat est la structure complète de la géométrie créée.

#### ROUTE API : `geometryUpdate`

Met à jour une géométrie existante éditable.

Paramètres :

```json
{
  "id": "geometry:17",
  "name": "Updated opening",
  "transform": {
    "space": "parent",
    "matrix": [
      1.0, 0.0, 0.0, 0.0,
      0.0, 1.0, 0.0, 0.0,
      0.0, 0.0, 1.0, 0.0,
      0.0, 0.0, 0.0, 1.0
    ]
  },
  "profile": [
    [0.0, 0.0],
    [1.0, 0.0],
    [1.0, 2.0],
    [0.0, 2.0]
  ],
  "depth": 0.4,
  "operation": "SUBTRACT"
}
```

| Paramètre   | Type                       | Obligatoire | Description             |
| ----------- | -------------------------- | ----------: | ----------------------- |
| `id`        | identifiant de géométrie   |         oui | Géométrie à modifier.   |
| `name`      | chaîne                     |         non | Nouveau nom.            |
| `transform` | `TransformInput`           |         non | Nouveau placement.      |
| `profile`   | `ProfileInput`             |         non | Nouveau profil complet. |
| `depth`     | nombre strictement positif |         non | Nouvelle profondeur.    |
| `operation` | chaîne                     |         non | `ADD` ou `SUBTRACT`.    |

Au moins un champ parmi `name`, `transform`, `profile`, `depth` et `operation` doit être présent.

Une modification de `operation` entraîne la suppression de la relation IFC précédente et la création de la relation correspondant à la nouvelle opération.

Le résultat est la structure complète de la géométrie après modification.

#### ROUTE API : `geometryDelete`

Supprime une géométrie et toutes les références correspondantes.

Paramètres exacts :

```json
{
  "id": "geometry:17"
}
```

Résultat :

```json
{
  "id": "geometry:17"
}
```

L’identifiant retourné n’est plus valide après la réponse.

### 12.4 Commandes de matériaux

#### ROUTE API : `materialGetAll`

Retourne la liste de tous les matériaux indexés.

Paramètres exacts :

```json
{}
```

Résultat :

```json
[
  {
    "id": "material:8",
    "ifcEntityId": 346,
    "globalId": null
  }
]
```

#### ROUTE API : `materialGet`

Retourne un matériau indexé.

Paramètres exacts :

```json
{
  "id": "material:8"
}
```

| Paramètre | Type                    | Description           |
| --------- | ----------------------- | --------------------- |
| `id`      | identifiant de matériau | Matériau à retourner. |

Le résultat respecte la structure définie dans la section 9.4.

#### ROUTE API : `materialCreate`

Crée un matériau simple.

Paramètres exacts :

```json
{
  "name": "Concrete",
  "category": "Concrete",
  "visual": {
    "color": [0.6, 0.6, 0.6],
    "opacity": 1.0,
    "metallic": 0.0,
    "roughness": 0.8
  },
  "properties": []
}
```

| Paramètre    | Type                       | Description                                          |
| ------------ | -------------------------- | ---------------------------------------------------- |
| `name`       | chaîne                     | Nom du matériau.                                     |
| `category`   | chaîne                     | Catégorie unique du matériau.                        |
| `visual`     | `VisualInput`              | Apparence visuelle complète.                         |
| `properties` | tableau de `PropertyInput` | Ensemble complet des propriétés éditables initiales. |

Tous les champs sont obligatoires.

Le résultat est la structure complète du matériau créé.

#### ROUTE API : `materialUpdate`

Met à jour un matériau simple éditable.

Paramètres :

```json
{
  "id": "material:8",
  "name": "Updated concrete",
  "category": "Concrete",
  "visual": {
    "color": [0.5, 0.5, 0.5],
    "opacity": 1.0,
    "metallic": 0.0,
    "roughness": 0.9
  },
  "properties": []
}
```

| Paramètre    | Type                       | Obligatoire | Description                                    |
| ------------ | -------------------------- | ----------: | ---------------------------------------------- |
| `id`         | identifiant de matériau    |         oui | Matériau à modifier.                           |
| `name`       | chaîne                     |         non | Nouveau nom.                                   |
| `category`   | chaîne                     |         non | Nouvelle catégorie.                            |
| `visual`     | `VisualInput`              |         non | Remplacement complet de l’apparence visuelle.  |
| `properties` | tableau de `PropertyInput` |         non | Remplacement complet des propriétés éditables. |

Au moins un champ parmi `name`, `category`, `visual` et `properties` doit être présent.

Lorsqu’il est présent, l’objet `visual` doit contenir ses quatre champs.

Le résultat est la structure complète du matériau après modification.

#### ROUTE API : `materialDelete`

Supprime un matériau et toutes ses affectations.

Paramètres exacts :

```json
{
  "id": "material:8"
}
```

Résultat :

```json
{
  "id": "material:8"
}
```

L’identifiant retourné n’est plus valide après la réponse.

#### ROUTE API : `materialAssign`

Affecte un matériau à un transform ou à une géométrie.

Paramètres exacts :

```json
{
  "materialId": "material:8",
  "targetId": "transform:42"
}
```

| Paramètre    | Type                                     | Description             |
| ------------ | ---------------------------------------- | ----------------------- |
| `materialId` | identifiant de matériau                  | Matériau à affecter.    |
| `targetId`   | identifiant de transform ou de géométrie | Cible de l’affectation. |

Résultat :

```json
{
  "materialId": "material:8",
  "targetId": "transform:42"
}
```

Une affectation déjà existante doit être considérée comme un succès sans créer de relation dupliquée.

#### ROUTE API : `materialUnassign`

Supprime une affectation précise entre un matériau et une cible.

Paramètres exacts :

```json
{
  "materialId": "material:8",
  "targetId": "transform:42"
}
```

| Paramètre    | Type                                     | Description        |
| ------------ | ---------------------------------------- | ------------------ |
| `materialId` | identifiant de matériau                  | Matériau concerné. |
| `targetId`   | identifiant de transform ou de géométrie | Cible concernée.   |

Résultat :

```json
{
  "materialId": "material:8",
  "targetId": "transform:42"
}
```

Si l’affectation n’existe pas, la commande retourne `INVALID_RELATION`.

---

## 13. Codes d’erreur

Les codes d’erreur autorisés sont :

```text
INVALID_JSON
UNKNOWN_COMMAND
INVALID_PARAMETERS
NO_MODEL_LOADED
FILE_NOT_FOUND
UNSUPPORTED_SCHEMA
IFC_PARSE_ERROR
IFC_WRITE_ERROR
ENTITY_NOT_FOUND
INVALID_ENTITY_TYPE
INVALID_RELATION
TRANSFORM_NOT_EDITABLE
GEOMETRY_NOT_EDITABLE
GEOMETRY_OPERATION_NOT_SUPPORTED
INVALID_PROFILE
INVALID_PLACEMENT
MATERIAL_NOT_EDITABLE
TESSELLATION_FAILED
INTERNAL_ERROR
```

### 13.1 `INVALID_JSON`

La ligne ne contient pas un document JSON valide ou sa racine n’est pas un objet JSON.

### 13.2 `UNKNOWN_COMMAND`

Le champ `command` ne correspond pas exactement à une commande autorisée.

### 13.3 `INVALID_PARAMETERS`

Les paramètres sont absents, incomplets, supplémentaires ou utilisent un type invalide.

### 13.4 `NO_MODEL_LOADED`

La commande nécessite une scène IFC mais aucun modèle n’est chargé.

### 13.5 `FILE_NOT_FOUND`

Le fichier demandé n’existe pas ou n’est pas accessible.

### 13.6 `UNSUPPORTED_SCHEMA`

Le schéma IFC du fichier n’est pas pris en charge par la compilation actuelle.

### 13.7 `IFC_PARSE_ERROR`

IfcOpenShell n’a pas pu charger le fichier.

### 13.8 `IFC_WRITE_ERROR`

IfcOpenShell n’a pas pu sauvegarder le fichier.

### 13.9 `ENTITY_NOT_FOUND`

L’identifiant de session n’existe pas dans la session courante.

### 13.10 `INVALID_ENTITY_TYPE`

L’identifiant existe mais ne correspond pas au type attendu par la commande.

### 13.11 `INVALID_RELATION`

La relation demandée est inexistante ou incompatible avec les entités.

### 13.12 `TRANSFORM_NOT_EDITABLE`

Le transform existe mais la propriété demandée ne peut pas être modifiée.

### 13.13 `GEOMETRY_NOT_EDITABLE`

La géométrie existe mais ne peut pas être modifiée par `geometryUpdate`.

### 13.14 `GEOMETRY_OPERATION_NOT_SUPPORTED`

L’opération géométrique demandée n’est pas compatible avec la cible ou le schéma.

### 13.15 `INVALID_PROFILE`

Le profil polygonal ne respecte pas les contraintes définies.

### 13.16 `INVALID_PLACEMENT`

Le placement ou la matrice fournie est invalide ou non représentable.

### 13.17 `MATERIAL_NOT_EDITABLE`

Le matériau existe mais sa structure ne peut pas être modifiée par l’API simplifiée.

### 13.18 `TESSELLATION_FAILED`

IfcOpenShell n’a pas pu produire la tessellation demandée.

### 13.19 `INTERNAL_ERROR`

Une erreur inattendue non couverte par un autre code s’est produite.

Les erreurs suivantes ne sont pas utilisées :

```text
ENTITY_STILL_REFERENCED
DELETE_NOT_ALLOWED
```

Une entité référencée reste supprimable.

Ses relations et références sont automatiquement nettoyées.

---

## 14. Structuration du code C++

### 14.1 Fichiers du projet

Le code C++ du programme est réparti dans les fichiers suivants :

```text
main.cpp
protocol.hpp
scene.hpp
transform.hpp
geometry.hpp
material.hpp
```

Aucun autre fichier source `.cpp` ne doit être créé.

Toutes les classes et leurs méthodes sont directement définies dans les fichiers `.hpp`.

Cette organisation évite la séparation classique entre fichiers d’en-tête et fichiers d’implémentation.

Les éventuels fichiers du système de compilation ne sont pas concernés par cette restriction.

### 14.2 Règles générales des fichiers `.hpp`

Chaque fichier `.hpp` commence par :

```cpp
#pragma once
```

Les fonctions libres et variables définies dans un fichier `.hpp` doivent être déclarées `inline` lorsque cela est nécessaire pour éviter les définitions multiples.

Les fichiers doivent respecter une séparation stricte des responsabilités.

La logique du protocole JSONL ne doit pas être mélangée avec la logique IFC.

Les classes `Transform`, `Geometry` et `Material` ne doivent jamais écrire directement sur `stdout` ou `stderr`.

Seul `main.cpp` contrôle les entrées et sorties du processus.

Aucun état global mutable ne doit être utilisé.

### 14.3 Dépendances

Les dépendances entre fichiers sont :

```text
main.cpp
├── protocol.hpp
└── scene.hpp
    ├── transform.hpp
    ├── geometry.hpp
    └── material.hpp
```

Les règles d’inclusion sont :

* `main.cpp` inclut `protocol.hpp` et `scene.hpp` ;
* `scene.hpp` inclut `transform.hpp`, `geometry.hpp` et `material.hpp` ;
* `protocol.hpp` ne doit pas inclure `scene.hpp` ;
* `transform.hpp` ne doit pas inclure `scene.hpp` ;
* `geometry.hpp` ne doit pas inclure `scene.hpp` ;
* `material.hpp` ne doit pas inclure `scene.hpp`.

Les classes métier peuvent recevoir des références ou pointeurs vers les objets IfcOpenShell nécessaires.

Elles ne doivent pas dépendre directement de la classe `IfcScene`.

Cette règle évite les inclusions circulaires.

Voici les librairies déjà installées sur le système pouvant être utilisées si besoin :

=== Static libraries === 
FOUND: /Users/quentin/.local/ifcopenshell/lib/libIfcParse.a /Users/quentin/.local/ifcopenshell/lib/libIfcParse.a: current ar archive arm64 
FOUND: /Users/quentin/.local/ifcopenshell/lib/libIfcGeom.a /Users/quentin/.local/ifcopenshell/lib/libIfcGeom.a: current ar archive arm64 
FOUND: /Users/quentin/.local/ifcopenshell/lib/libgeometry_kernel_opencascade.a /Users/quentin/.local/ifcopenshell/lib/libgeometry_kernel_opencascade.a: current ar archive arm64 

=== Required headers === 
FOUND: /Users/quentin/.local/ifcopenshell/include/ifcparse/IfcFile.h 
FOUND: /Users/quentin/.local/ifcopenshell/include/ifcparse/IfcSchema.h 
FOUND: /Users/quentin/.local/ifcopenshell/include/ifcgeom/Iterator.h 
FOUND: /Users/quentin/.local/ifcopenshell/include/ifcgeom/ConversionSettings.h 
FOUND: /Users/quentin/.local/ifcopenshell/include/ifcgeom/kernels/opencascade/OpenCascadeKernel.h 

=== Compiled IFC schemas === 
set(IFCOPENSHELL_SCHEMA_VERSIONS 2x3;4;4x1;4x2;4x3;4x3_tc1;4x3_add1;4x3_add2) 

=== OpenCascade support === 
set(IFCOPENSHELL_WITH_OPENCASCADE ON) if(IFCOPENSHELL_WITH_OPENCASCADE) 

=== Exported CMake targets === 
/Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::IFCOPENSHELL_CGAL INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::IfcParse STATIC IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_kernel_opencascade STATIC IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_kernel_cgal STATIC IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_kernel_cgal_simple STATIC IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_serializer STATIC IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_serializer_ifc2x3 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_serializer_ifc4 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_serializer_ifc4x1 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_serializer_ifc4x2 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_serializer_ifc4x3 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_serializer_ifc4x3_tc1 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_serializer_ifc4x3_add1 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_serializer_ifc4x3_add2 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_mapping_ifc2x3 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_mapping_ifc4 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_mapping_ifc4x1 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_mapping_ifc4x2 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_mapping_ifc4x3 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_mapping_ifc4x3_tc1 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_mapping_ifc4x3_add1 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::geometry_mapping_ifc4x3_add2 INTERFACE IMPORTED) /Users/quentin/.local/ifcopenshell/lib/cmake/IfcOpenShell/IfcOpenShellTargets.cmake:add_library(IfcOpenShell::IfcGeom STATIC IMPORTED)

### 14.4 `main.cpp`

`main.cpp` constitue l’unique point d’entrée de l’exécutable.

Il contient :

```cpp
int main();
```

Ses responsabilités sont :

* instancier une unique instance de `IfcScene` ;
* lire `stdin` ligne par ligne avec `std::getline` ;
* ignorer les lignes entièrement vides ;
* transmettre chaque ligne à `JsonlProtocol` ;
* vérifier la structure générale de la requête ;
* vérifier le nom exact de la commande ;
* vérifier les paramètres exacts de chaque commande ;
* rejeter les champs supplémentaires ;
* dispatcher la commande vers `IfcScene` ;
* convertir le résultat en réponse JSONL ;
* convertir les erreurs en réponses normalisées ;
* écrire les réponses sur `stdout` ;
* écrire les logs techniques sur `stderr` ;
* flusher `stdout` après chaque réponse ;
* terminer la boucle après une commande `exit` valide.

`main.cpp` gère exclusivement :

* le protocole ;
* la validation ;
* le dispatch ;
* les erreurs ;
* les entrées et sorties ;
* la durée de vie de la scène.

`main.cpp` ne doit pas contenir :

* de logique de création d’entités IFC ;
* de logique de placement IFC ;
* de logique géométrique ;
* de logique de matériau ;
* de parcours métier du graphe IFC ;
* de suppression directe d’entités IFC.

Le dispatch doit utiliser uniquement les noms exacts définis dans la section 12.

Aucune recherche approximative ou normalisation du nom de commande ne doit être effectuée.

### 14.5 `protocol.hpp`

`protocol.hpp` prend en charge le parsing et la sérialisation du protocole JSONL.

Il définit au minimum les types suivants :

```text
ProtocolRequest
ProtocolError
JsonlProtocol
```

#### `ProtocolRequest`

`ProtocolRequest` représente une requête JSONL déjà parsée.

Il contient les valeurs suivantes :

```text
id
command
params
```

#### `ProtocolError`

`ProtocolError` représente une erreur normalisée.

Il contient :

```text
code
message
details
```

#### `JsonlProtocol`

`JsonlProtocol` fournit au minimum les opérations conceptuelles suivantes :

```text
parseRequest
createSuccessResponse
createErrorResponse
serializeResponse
```

`parseRequest` vérifie :

* que la ligne contient un document JSON valide ;
* que la racine est un objet ;
* que les champs `id`, `command` et `params` existent ;
* que `id` est une chaîne non vide ;
* que `command` est une chaîne non vide ;
* que `params` est un objet ;
* qu’aucun champ supplémentaire de premier niveau n’existe.

La validation spécifique des paramètres de chaque commande est effectuée par `main.cpp` avant le dispatch.

`protocol.hpp` ne doit contenir :

* aucune dépendance à IfcOpenShell ;
* aucune logique métier ;
* aucun accès direct à la scène ;
* aucun dispatch de commande.

Une seule bibliothèque JSON doit être utilisée dans l’ensemble du projet.

Aucune seconde implémentation du parsing ou de la sérialisation JSON ne doit exister.

### 14.6 `scene.hpp`

`scene.hpp` définit la classe principale :

```text
IfcScene
```

`IfcScene` représente la session IFC actuellement chargée.

Elle possède et contrôle :

* le fichier IFC chargé en mémoire ;
* l’état indiquant si une scène est chargée ;
* le chemin du fichier actuellement chargé ;
* le schéma IFC courant ;
* la génération des identifiants de session ;
* l’index des transforms ;
* l’index des géométries ;
* l’index des matériaux ;
* les correspondances entre identifiants de session et entités IFC ;
* les relations entre transforms, géométries et matériaux ;
* les opérations de suppression récursive ;
* les caches de tessellation éventuels.

`IfcScene` est l’unique propriétaire du modèle IFC chargé.

Les classes `Transform`, `Geometry` et `Material` ne sont pas propriétaires du fichier IFC.

`IfcScene` fournit une opération correspondant à chaque commande métier :

```text
getCapabilities
load
save

transformGetAll
transformGet
transformCreate
transformUpdate
transformDelete
transformTessellate

geometryGetAll
geometryGet
geometryCreate
geometryUpdate
geometryDelete

materialGetAll
materialGet
materialCreate
materialUpdate
materialDelete
materialAssign
materialUnassign
```

La commande `exit` est gérée par `main.cpp`.

La commande `help` peut être gérée par `main.cpp` ou par une fonction statique du protocole.

Elle ne doit pas dépendre du modèle IFC chargé.

`IfcScene` doit vérifier qu’un modèle est chargé avant toute opération nécessitant une scène.

Lors d’un nouvel appel à `load`, `IfcScene` doit :

1. détruire proprement la session précédente ;
2. vider les index ;
3. invalider les anciens wrappers ;
4. réinitialiser la génération des identifiants ;
5. charger le nouveau fichier IFC ;
6. détecter son schéma ;
7. construire l’index des transforms ;
8. construire l’index des géométries ;
9. construire l’index des matériaux ;
10. construire les relations entre les objets.

`IfcScene` coordonne toutes les opérations impliquant plusieurs catégories d’objets.

Cela comprend notamment :

* la suppression récursive d’un transform ;
* la suppression des références vers une géométrie ;
* l’affectation d’un matériau ;
* la suppression d’un matériau et de ses affectations ;
* la résolution des transforms mondiaux ;
* la tessellation finale ;
* le nettoyage des objets IFC orphelins ;
* la mise à jour des index après une modification.

### 14.7 `transform.hpp`

`transform.hpp` définit la classe :

```text
Transform
```

Une instance de `Transform` représente un transform indexé dans la session.

Elle stocke ou permet de retrouver :

* son identifiant de session ;
* une référence non propriétaire vers son entité IFC ;
* son identifiant STEP éventuel ;
* son `GlobalId` éventuel ;
* son nom ;
* son placement ;
* ses identifiants parents ;
* ses identifiants de géométries ;
* ses identifiants de matériaux ;
* son caractère éditable ;
* sa définition IFC en lecture.

La classe `Transform` est responsable de la logique propre à un transform :

* lire son nom ;
* lire ses attributs exposés ;
* lire ses propriétés éditables ;
* produire sa structure JSON ;
* lire son placement local ;
* fournir les données nécessaires au calcul du placement mondial ;
* modifier son nom ;
* modifier son placement ;
* remplacer ses relations parentes ;
* remplacer ses propriétés éditables.

La classe `Transform` ne doit pas :

* lire directement les requêtes JSONL ;
* écrire une réponse JSONL ;
* supprimer récursivement d’autres transforms ;
* gérer les index globaux ;
* affecter directement un matériau ;
* tesseller récursivement une hiérarchie complète.

Ces opérations sont coordonnées par `IfcScene`.

### 14.8 `geometry.hpp`

`geometry.hpp` définit la classe :

```text
Geometry
```

Une instance de `Geometry` représente :

* une géométrie IFC native indexée ;
* une opération additive créée par l’API ;
* une opération soustractive créée par l’API.

Elle stocke ou permet de retrouver :

* son identifiant de session ;
* une référence non propriétaire vers son entité IFC principale ;
* son transform parent ;
* son type IFC ;
* sa source `ifc` ou `editor` ;
* son caractère éditable ;
* son opération `BASE`, `ADD` ou `SUBTRACT` ;
* son profil éventuel ;
* sa profondeur éventuelle ;
* son placement éventuel ;
* ses matériaux associés.

La classe `Geometry` est responsable de :

* lire les informations d’une géométrie ;
* produire sa structure JSON ;
* valider un profil polygonal ;
* créer un `IfcExtrudedAreaSolid` ;
* créer une opération `ADD` ;
* créer une opération `SUBTRACT` ;
* modifier une opération créée par l’API ;
* remplacer le profil d’une extrusion ;
* remplacer la profondeur d’une extrusion ;
* remplacer le placement d’une extrusion ;
* convertir une opération `ADD` en `SUBTRACT` ;
* convertir une opération `SUBTRACT` en `ADD` ;
* fournir les entités nécessaires à la tessellation.

Une opération `ADD` utilise exclusivement :

```text
IfcProjectionElement
IfcRelProjectsElement
IfcExtrudedAreaSolid
```

Une opération `SUBTRACT` utilise exclusivement :

```text
IfcOpeningElement
IfcRelVoidsElement
IfcExtrudedAreaSolid
```

La classe `Geometry` ne doit pas :

* modifier une géométrie native déclarée non éditable ;
* gérer les identifiants de session globaux ;
* supprimer seule des objets potentiellement partagés ;
* écrire sur les flux du processus.

Les suppressions et le nettoyage des références sont coordonnés par `IfcScene`.

### 14.9 `material.hpp`

`material.hpp` définit la classe :

```text
Material
```

Une instance de `Material` représente un matériau IFC indexé.

Elle stocke ou permet de retrouver :

* son identifiant de session ;
* une référence non propriétaire vers l’entité IFC de matériau ;
* son identifiant STEP éventuel ;
* son nom ;
* sa catégorie ;
* son caractère éditable ;
* sa représentation visuelle simplifiée ;
* ses propriétés éditables ;
* les données non éditables retournées dans `definition`.

La classe `Material` est responsable de :

* lire un matériau IFC ;
* produire sa structure JSON ;
* créer un matériau simple ;
* créer sa représentation visuelle ;
* remplacer son nom ;
* remplacer sa catégorie ;
* remplacer son apparence visuelle ;
* remplacer ses propriétés éditables ;
* identifier les entités IFC exclusivement associées au matériau.

La classe `Material` ne doit pas :

* gérer les index de transforms ;
* gérer les index de géométries ;
* décider seule du type d’une cible ;
* supprimer directement toutes les affectations du graphe ;
* écrire sur les flux du processus.

Les opérations suivantes sont coordonnées par `IfcScene` :

```text
materialAssign
materialUnassign
materialDelete
```

### 14.10 Ownership C++

`IfcScene` est propriétaire des instances C++ suivantes :

```text
Transform
Geometry
Material
```

Ces instances sont stockées dans des conteneurs indexés par leurs identifiants de session.

Les références ou pointeurs vers les entités IfcOpenShell sont non propriétaires.

La durée de vie des wrappers C++ ne doit jamais dépasser celle du fichier IFC chargé.

Lors d’un changement de scène :

1. les wrappers sont invalidés ou détruits ;
2. les index sont vidés ;
3. le fichier IFC précédent est détruit ;
4. le nouveau fichier est chargé ;
5. les nouveaux wrappers sont créés.

Aucune classe métier ne doit conserver une référence vers une entité IFC détruite.

### 14.11 Mise à jour des index

Après toute création, modification ou suppression, `IfcScene` doit mettre à jour les index concernés.

Une commande ne doit jamais retourner avant que les index correspondent au graphe IFC modifié.

Les identifiants des entités non supprimées doivent rester stables pendant la session.

Une modification interne du graphe IFC ne doit pas entraîner la régénération globale des identifiants de session.

### 14.12 Gestion interne des erreurs

Les erreurs métier peuvent être représentées par une exception dédiée contenant :

```text
code
message
details
```

`main.cpp` intercepte cette exception et utilise `JsonlProtocol` pour produire la réponse JSONL correspondante.

Les exceptions standards ou erreurs inattendues sont converties en :

```text
INTERNAL_ERROR
```

Les détails techniques peuvent être écrits sur `stderr`.

Ils ne doivent pas modifier la structure JSONL retournée.

Une erreur ne doit pas arrêter le processus, sauf si l’état interne ne permet plus de poursuivre sans risque.

### 14.13 Absence de comportement implicite

L’implémentation ne doit ajouter aucun comportement non décrit dans cette spécification.

Elle ne doit notamment pas :

* créer automatiquement un parent manquant ;
* choisir automatiquement une autre commande ;
* corriger un profil invalide ;
* ajouter physiquement le premier point à la fin du profil ;
* convertir automatiquement une chaîne en nombre ;
* remplacer automatiquement un identifiant invalide ;
* utiliser une valeur par défaut pour un champ obligatoire ;
* accepter des paramètres inconnus ;
* accepter une ancienne variante du protocole ;
* accepter plusieurs formes JSON pour une même commande ;
* ignorer silencieusement une erreur de relation ;
* modifier automatiquement une entité non éditable.

Lorsqu’une donnée obligatoire est absente ou invalide, la commande doit échouer avec une erreur explicite.

---

## 15. Règles fondamentales

1. Le fichier IFC chargé reste le modèle maître.
2. La sauvegarde ne reconstruit jamais le fichier depuis les index simplifiés.
3. Les données IFC non prises en charge sont conservées, sauf conséquence directe d’une suppression explicite.
4. Le protocole JSONL est strict et normatif.
5. Une seule structure JSON est acceptée pour chaque commande.
6. Les noms de commandes et de champs sont sensibles à la casse.
7. Les champs supplémentaires sont interdits.
8. Les conversions implicites de types sont interdites.
9. Les géométries créées par l’API utilisent exclusivement des profils polygonaux extrudés.
10. L’extrusion est toujours réalisée selon l’axe local positif Z.
11. La position et la direction de l’extrusion sont déterminées par le transform de la géométrie.
12. Les ajouts utilisent `IfcProjectionElement`.
13. Les soustractions utilisent `IfcOpeningElement`.
14. Lorsqu’un tableau de propriétés est fourni, il remplace intégralement les propriétés éditables existantes.
15. Une propriété éditable absente du nouveau tableau est supprimée du graphe IFC.
16. Les propriétés non éditables restent conservées dans `definition`.
17. Les commandes de suppression sont destructives et ne demandent aucune confirmation.
18. La suppression d’un transform entraîne la suppression récursive de ses enfants.
19. Toute relation référençant une entité supprimée est retirée ou supprimée.
20. Après une suppression, le graphe IFC ne doit contenir aucune référence invalide.
21. Les identifiants de session ne sont valides que pendant la session courante.
22. Les identifiants des entités non supprimées restent stables pendant la session.
23. `IfcScene` est l’unique propriétaire du fichier IFC chargé.
24. Seul `main.cpp` lit `stdin` et écrit sur `stdout`.
25. Les classes métier ne contiennent aucune logique de protocole JSONL.
26. Toutes les classes sont directement définies dans les fichiers `.hpp`.
27. Aucun fichier source `.cpp` autre que `main.cpp` n’est utilisé.
28. Aucun comportement implicite non documenté ne doit être ajouté.
