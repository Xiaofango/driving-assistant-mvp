"""Scene population mixin: hero vehicle, demo routes, NPC traffic, pedestrians, riders."""

from .carla_config import FOLLOW_DISTANCE_METERS, RIDER_TARGET_COUNT, WALKER_TARGET_COUNT


class SceneMixin:
    """Actors and route geometry created for the demonstration scene."""

    def _vehicle_blueprint(self):
        blueprints = self.world.get_blueprint_library().filter("vehicle.*")
        for name in ("vehicle.tesla.model3", "vehicle.audi.tt", "vehicle.lincoln.mkz_2020"):
            match = next((bp for bp in blueprints if bp.id == name), None)
            if match is not None:
                return match
        four_wheel = [bp for bp in blueprints if not bp.has_attribute("number_of_wheels")
                      or bp.get_attribute("number_of_wheels").as_int() == 4]
        if not four_wheel:
            raise RuntimeError("CARLA 地图没有可用的四轮车辆蓝图")
        return four_wheel[0]

    def _spawn_scene(self):
        points = self.map.get_spawn_points()
        if len(points) < 3:
            raise RuntimeError("当前 CARLA 地图的车辆出生点不足")
        bp = self._vehicle_blueprint()
        if bp.has_attribute("role_name"):
            bp.set_attribute("role_name", "hero")
        for point in points[:min(len(points), 18)]:
            self.ego = self.world.try_spawn_actor(bp, point)
            if self.ego is not None:
                self.owned_actors.append(self.ego)
                break
        if self.ego is None:
            raise RuntimeError("CARLA 无法生成演示车辆；请换一个地图或清理占用出生点的车辆")
        start = self.ego.get_location()
        candidates = sorted((p for p in points if p.location.distance(start) > 150),
                            key=lambda p: abs(p.location.distance(start) - 450))
        if not candidates:
            raise RuntimeError("地图太小，无法配置两条演示路线")
        self.destination = candidates[0].location
        direct = start.distance(self.destination)
        vias = [p.location for p in points
                if p.location.distance(start) > 80 and p.location.distance(self.destination) > 80]
        if not vias:
            raise RuntimeError("地图中没有可用的途经点")
        self.via = min(vias, key=lambda v: abs(
            (start.distance(v) + v.distance(self.destination)) / direct - 1.55))
        if start.distance(self.via) + self.via.distance(self.destination) < direct * 1.15:
            raise RuntimeError("找不到足够不同的候选路线")
        self.ego.set_autopilot(True, self.tm_port)
        self.tm.distance_to_leading_vehicle(self.ego, FOLLOW_DISTANCE_METERS[self.follow_level])
        self.tm.vehicle_percentage_speed_difference(self.ego, 10.0)
        self.tm.set_path(self.ego, [self.via, self.destination])
        nearby = sorted((p for p in points if p.location.distance(start) > 28),
                        key=lambda p: p.location.distance(start))
        for point in nearby[:3]:
            npc_bp = self._vehicle_blueprint()
            if npc_bp.has_attribute("role_name"):
                npc_bp.set_attribute("role_name", "autopilot")
            actor = self.world.try_spawn_actor(npc_bp, point)
            if actor is not None:
                self.owned_actors.append(actor)
                actor.set_autopilot(True, self.tm_port)
        self._spawn_camera()

    def _spawn_walkers(self):
        """Populate sidewalks with AI pedestrians and put riders into traffic."""
        library = self.world.get_blueprint_library()
        walker_blueprints = [bp for bp in library.filter("walker.pedestrian.*")
                             if bp.has_attribute("is_invincible")]
        controller_bp = library.find("controller.ai.walker")
        ego_location = self.ego.get_location()
        spawned = 0
        attempts = 0
        while spawned < WALKER_TARGET_COUNT and attempts < WALKER_TARGET_COUNT * 3:
            attempts += 1
            location = self.world.get_random_location_from_navigation()
            if location is None or location.distance(ego_location) > 150:
                continue
            blueprint = walker_blueprints[spawned % len(walker_blueprints)] if walker_blueprints else None
            if blueprint is None:
                break
            blueprint.set_attribute("is_invincible", "false")
            walker = self.world.try_spawn_actor(blueprint, self.carla.Transform(location))
            if walker is None:
                continue
            controller = self.world.try_spawn_actor(controller_bp, self.carla.Transform(),
                                                    attach_to=walker)
            self.owned_actors.append(walker)
            if controller is None:
                continue
            self.owned_actors.append(controller)
            self.walker_controllers.append(controller)
            try:
                controller.start()
                destination = self.world.get_random_location_from_navigation()
                if destination is not None:
                    controller.go_to_location(destination)
                speed = 1.2
                if blueprint.has_attribute("speed"):
                    speed = blueprint.get_attribute("speed").as_float()
                controller.set_max_speed(min(max(speed, 0.8), 2.0))
            except Exception as exc:
                self._sensor_errors.append(f"walker:{walker.id}: {type(exc).__name__}: {exc}")
            spawned += 1

        riders = 0
        for blueprint in library.filter("vehicle.*"):
            if riders >= RIDER_TARGET_COUNT:
                break
            if not blueprint.has_attribute("number_of_wheels") or \
                    blueprint.get_attribute("number_of_wheels").as_int() != 2:
                continue
            for point in self.map.get_spawn_points():
                if riders >= RIDER_TARGET_COUNT:
                    break
                if point.location.distance(ego_location) < 60 or point.location.distance(ego_location) > 150:
                    continue
                if blueprint.has_attribute("role_name"):
                    blueprint.set_attribute("role_name", "rider")
                actor = self.world.try_spawn_actor(blueprint, point)
                if actor is None:
                    continue
                self.owned_actors.append(actor)
                self._rider_ids.add(actor.id)
                actor.set_autopilot(True, self.tm_port)
                riders += 1

    def _eta(self, start, points) -> float:
        locations = [start, *points]
        meters = sum(a.distance(b) for a, b in zip(locations, locations[1:]))
        return round(meters / 500.0, 1)
