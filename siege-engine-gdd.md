# Siege Engine

## Game Design Document / AI Build Brief

### Project Type

Browser-based 3D physics destruction game.

### Target Platform

Single-page HTML game using Three.js, realistic graphics, and rigid body physics.

### Core Tech Stack

- Three.js for rendering
- Rapier.js or Ammo.js for rigid body physics
- Vite or simple static HTML/JS build
- GLTF/GLB assets where useful
- WebGL first; WebGPU optional
- No backend required for MVP

### Important Implementation Note

Do not attempt true fully dynamic mesh fracturing for the first version. Use modular, pre-fractured, physics-enabled castle components with structural integrity rules. This will look realistic, run in the browser, and still allow convincing collapses.

---

## High Concept

The player controls a medieval siege weapon, either a catapult or trebuchet, and attempts to destroy a fortified castle using limited ammunition.

The castle is physically simulated. Walls, towers, roofs, floors, bridges, and buildings can be damaged, broken apart, destabilized, and collapsed by gravity. The castle also contains inhabitants living or hiding in internal areas.

The player wins by either:

1. Destroying 100% of the castle structure, or
2. Eliminating all inhabitants using the available ammunition.

The player receives bonus ammunition for excellent shots that cause major structural damage, collapse large sections, or eliminate many inhabitants at once.

---

## Tone and Visual Style

### Visual Target

Realistic medieval siege simulation.

### Style

- Grounded, gritty medieval realism
- Stone, wood, dirt, dust, smoke, debris
- No cartoon visuals
- No fantasy magic
- No exaggerated arcade effects

### Violence Presentation

- Non-graphic
- Inhabitants can be represented as small medieval civilians/guards or abstracted figures
- No gore required
- Death/elimination should be communicated through ragdoll, disappearance, icon marker, or score notification

---

## Core Gameplay Loop

1. Player surveys the castle.
2. Player selects ammunition type.
3. Player adjusts siege engine:
   - Horizontal aim
   - Launch angle
   - Pull strength / counterweight tension
   - Optional projectile spin or fuse timing depending on ammo
4. Player fires.
5. Projectile follows physics trajectory.
6. Projectile impacts castle, terrain, or inhabitants.
7. Damage is calculated:
   - Direct impact damage
   - Structural stress
   - Collapse damage
   - Secondary debris impacts
8. Castle sections may break loose and fall.
9. Inhabitants may be eliminated by direct hits, collapse, fire, or falling debris.
10. Player earns score and possibly bonus ammo.
11. Repeat until ammo is gone or victory condition is met.

---

## Player-Controlled Siege Weapons

### Minimum MVP

Implement one siege weapon first: trebuchet or catapult.

### Preferred Full Version

The player can switch between:

#### Catapult

- Lower range
- Faster reload
- More direct arc
- Better for smashing walls at medium range

#### Trebuchet

- Longer range
- Slower reload
- Higher arc
- Better for hitting towers, roofs, courtyards, and internal living areas

---

## Controls

### Basic Controls

- A / D or mouse drag: rotate siege engine left/right
- W / S or slider: adjust launch angle
- Power slider: adjust launch force
- Ammo selector UI
- Fire button / Spacebar

### Camera Controls

- Orbit around siege engine
- Follow projectile
- Free camera after shot
- Castle inspection mode

### Optional Advanced Controls

- Counterweight mass for trebuchet
- Sling release timing
- Wind toggle for harder difficulty
- Projectile camera replay

---

## Ammunition Types

Each ammo type should have different mass, radius, damage behavior, and special effects.

### Stone Ball

- Standard ammo
- Heavy blunt damage
- Good against walls and towers
- Medium splash debris force

### Boulder

- Very heavy
- Lower range
- Massive structural impact
- Can break wall bases and collapse towers

### Fire Pot

- Lower impact damage
- Starts fires in wooden structures
- Damages inhabitants over time in living areas
- Can spread through roofs, stables, and wooden supports

### Scatter Shot

- Breaks into smaller stones mid-flight or on impact
- Good against exposed inhabitants
- Poor against heavy walls

### Iron Shot

- Dense, high penetration
- Good against gates, towers, and support columns
- Rare ammo

### Explosive Barrel, optional late-game

- High area damage
- Can trigger chain collapses
- Should be scarce to avoid trivializing the game

---

## Castle Design

The castle should be modular and destructible.

### Major Sections

- Outer curtain walls
- Gatehouse
- Corner towers
- Inner keep
- Wooden roofs
- Courtyard
- Barracks
- Great hall
- Storage buildings
- Living quarters
- Walkways and battlements

### Structural Model

The castle should be built from many physics-aware modules:

- Wall blocks
- Wall panels
- Tower rings
- Floors
- Roof beams
- Support columns
- Gate doors
- Battlement chunks
- Interior rooms

Each module should have:

- Health
- Mass
- Physics body
- Structural connections to nearby modules
- Support dependency score
- Collapse threshold
- Material type: stone, wood, dirt, metal

Damage should not simply delete objects. Instead:

- Low damage: cracks, dust, small debris
- Medium damage: chunks detach
- High damage: module becomes dynamic rigid body
- Loss of support: upper structures collapse due to gravity

---

## Recommended Destruction System

Use a hybrid system:

### 1. Pre-fractured Geometry

Walls are made from visible chunks or panels. Damaged chunks can detach and become rigid bodies.

### 2. Structural Integrity Graph

Each castle piece knows what supports it. If base supports are destroyed, upper pieces lose stability. Unsupported pieces become dynamic and fall.

### 3. Damage Propagation

Impacts apply damage to nearby connected pieces. Shock damage spreads through neighboring modules. Heavy projectiles damage deeper into structures.

### 4. Collapse Simulation

Once pieces detach, the physics engine handles falling, collisions, and secondary damage.

This approach is better than full real-time fracturing in the browser.

---

## Damage Rules

Projectile impact should consider:

- Projectile mass
- Velocity
- Impact angle
- Material type
- Hit location
- Existing damage
- Whether the target is load-bearing

Example damage formula:

```text
damage = projectileMass * velocityMagnitude * materialMultiplier * impactAngleMultiplier
```

Structural damage should be higher when hitting:

- Wall bases
- Tower foundations
- Support columns
- Gate hinges
- Wooden beams
- Corners and joints

---

## Gravity Collapse

The game must support emergent collapse.

Examples:

- Destroying the base of a tower causes upper tower pieces to fall.
- Breaking a wall foundation causes battlements to collapse.
- Destroying support beams causes a roof to cave in.
- Fallen tower debris can crush nearby buildings or inhabitants.
- A collapsing wall can breach adjacent walls.

---

## Castle Population

The castle contains inhabitants distributed across living and working areas.

### Inhabitant Types

- Civilians
- Guards
- Workers
- Nobles, optional high-value targets

### Placement Areas

- Barracks
- Towers
- Courtyard
- Great hall
- Living quarters
- Gatehouse
- Walkways

### MVP Behavior

- Inhabitants can idle or walk simple paths.
- They can be eliminated by direct projectile hits, falling debris, collapse zones, or fire.
- They do not need advanced AI.

### Optional Behavior

- Panic when a nearby impact occurs
- Run away from damaged structures
- Seek shelter
- Evacuate collapsing buildings
- Guards move to battlements

---

## Population Tracking

The UI should show:

- Total inhabitants
- Remaining inhabitants
- Inhabitants eliminated this shot
- Inhabitants eliminated by collapse
- Inhabitants eliminated by fire
- Inhabitants inside unstable structures

---

## Victory Conditions

The player wins if either condition is met:

### Castle Destruction Victory

Castle destruction reaches 100%, based on total structural value destroyed, not just visual objects removed.

### Population Victory

All inhabitants are eliminated.

The player loses if:

- Ammunition reaches zero
- Castle destruction is below 100%
- At least one inhabitant remains

---

## Scoring System

Score should reward:

- Direct structural damage
- Collapse chains
- Multi-elimination shots
- Efficient ammo usage
- Hitting weak points
- Destroying high-value structures
- Eliminating inhabitants inside buildings via collapse

Suggested score categories:

- Structural damage score
- Collapse bonus
- Population impact bonus
- Precision bonus
- Ammo efficiency bonus
- Optional overkill penalty

---

## Bonus Ammunition

The player earns bonus ammo for excellent shots.

Examples:

- Destroy 15%+ castle integrity in one shot: +1 stone ball
- Collapse a tower: +1 boulder
- Eliminate 10+ inhabitants in one shot: +1 scatter shot
- Cause chain collapse across 3+ connected structures: +1 iron shot
- Destroy gatehouse with one shot: +1 explosive/fire ammo
- Perfect weak-point hit: random bonus ammo

Avoid giving too much ammo. Bonus ammo should feel earned.

---

## UI Requirements

### Main HUD

- Ammo remaining
- Selected ammo type
- Power meter
- Angle indicator
- Optional wind indicator
- Castle destruction percentage
- Population remaining
- Score
- Shot number

### Shot Result Popup

- Damage caused
- Structures destroyed
- Inhabitants eliminated
- Collapse bonus
- Bonus ammo earned

### Camera/UI Modes

- Aim mode
- Projectile follow mode
- Impact replay mode
- Free inspection mode
- Damage report overlay

---

## Graphics Requirements

### Environment

- Medieval landscape
- Castle on hill or flat battlefield
- Dirt ground
- Skybox
- Sunlight
- Fog or atmospheric haze
- Dust clouds on impact
- Smoke from fires

### Castle Visuals

- Stone walls with normal maps
- Cracks appear after damage
- Broken edges and debris
- Wooden beams splinter
- Dust puffs from impacts
- Fire/smoke on burning structures

### Projectile Visuals

- Optional motion blur or trail
- Impact particles
- Debris spray
- Camera shake on major impacts

---

## Performance Requirements

The game must run in a browser.

Target:

- 60 FPS on a decent desktop GPU
- 30 FPS minimum during large collapses

Performance strategies:

- Use instanced meshes for repeated stones/debris
- Keep active rigid bodies limited
- Sleep inactive physics objects
- Convert far debris to static after settling
- Use simplified collider shapes
- Use LOD for castle pieces
- Avoid thousands of dynamic rigid bodies
- Use pooled particles
- Limit maximum debris count

---

## Physics Requirements

Use rigid body physics for:

- Projectiles
- Castle chunks
- Falling debris
- Optional siege weapon arm movement
- Optional inhabitants as capsules or simple ragdolls

Recommended physics engine:

Use Rapier.js unless there is a strong reason to use Ammo.js.

Rapier advantages:

- Fast
- Modern WASM physics
- Good rigid body support
- Easier setup than Ammo.js

Required physics features:

- Gravity
- Rigid body collision
- Projectile trajectories
- Impact impulses
- Dynamic/static body switching
- Collision events
- Sleeping bodies
- Compound colliders

---

## Suggested Object Model

### GameState

- currentAmmo
- ammoInventory
- score
- shotCount
- castleIntegrity
- inhabitantsRemaining
- gameStatus

### Projectile

- type
- mass
- radius
- damageMultiplier
- areaDamageRadius
- specialEffect

### CastlePiece

- id
- mesh
- rigidBody
- collider
- materialType
- health
- maxHealth
- structuralValue
- isLoadBearing
- supportLinks
- supportedBy
- stability
- detached

### Inhabitant

- id
- position
- currentRoom
- health
- alive
- panicState
- value

### StructureGraph

- nodes: CastlePiece[]
- edges: support relationships
- methods:
  - applyDamage()
  - recalculateSupport()
  - detachUnstablePieces()
  - computeIntegrity()

---

## Minimum Viable Product

The first playable version should include:

1. One trebuchet or catapult
2. One castle with:
   - Two walls
   - One gatehouse
   - Two towers
   - One inner building
3. Three ammo types:
   - Stone ball
   - Boulder
   - Fire pot
4. Basic aiming and firing
5. Physics projectile trajectory
6. Modular castle damage
7. Gravity-based collapse
8. Population placed in rooms
9. Basic win/loss conditions
10. Score and bonus ammo system

Do not start by building every feature. First prove:

- Projectile can hit castle
- Castle modules can detach
- Base damage can cause collapse
- Inhabitants can be eliminated by impact/collapse
- Game can be won or lost

---

## Stretch Goals

- Multiple siege weapons
- Multiple castle layouts
- Campaign levels
- Upgrade system
- Slow-motion replay
- Wind and weather
- Better inhabitant AI
- Fire spread simulation
- Procedural castle generation
- Online leaderboard
- Sandbox mode
- Level editor

---

## AI Build Instructions

Build this as a browser game using Three.js.

Start with a working MVP, not a huge architecture.

The first deliverable must be a playable local build with:

- `npm install`
- `npm run dev`
- Browser opens the game
- Player can aim and fire at a destructible castle
- Castle damage and collapse are visible
- Win/loss state works

Implementation priorities:

1. Physics correctness
2. Destruction and collapse feel
3. Playability
4. Graphics polish
5. Advanced AI/population behavior

Do not fake the core destruction with only animations. Castle pieces must use rigid body physics once detached.

Do not attempt full real-time mesh fracturing in version one. Use modular/pre-fractured pieces.

---

## Recommended File Structure

```text
/src
  main.js
  game/
    Game.js
    GameState.js
    ScoringSystem.js
  physics/
    PhysicsWorld.js
    CollisionHandlers.js
  siege/
    SiegeEngine.js
    Catapult.js
    Trebuchet.js
    Projectile.js
    AmmoTypes.js
  castle/
    Castle.js
    CastlePiece.js
    StructureGraph.js
    DamageSystem.js
  population/
    Inhabitant.js
    PopulationManager.js
  rendering/
    SceneSetup.js
    CameraController.js
    EffectsManager.js
  ui/
    HUD.js
    ShotReport.js
  assets/
    models/
    textures/
```

---

## Acceptance Criteria

The game is acceptable when:

- Player can launch projectiles with adjustable aim and power.
- Projectiles follow believable physics arcs.
- Castle pieces take damage based on impact force.
- Damaged pieces detach and become dynamic rigid bodies.
- Destroying lower support sections can cause upper sections to collapse.
- Falling debris can damage other castle parts and inhabitants.
- Castle destruction percentage updates correctly.
- Population count updates correctly.
- Player can win by destroying the castle or eliminating all inhabitants.
- Bonus ammo is awarded for high-impact shots.
- Game runs in a modern browser without a server backend.

---

## One-Sentence Pitch

A realistic browser-based medieval siege physics game where players use catapults and trebuchets to destroy a populated castle through precision shots, structural collapse, and limited-ammunition strategy.
