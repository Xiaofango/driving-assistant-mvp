"""CARLA-specific constants shared by the gateway mixins."""

# TM follow-distance levels (level -> meters).
FOLLOW_DISTANCE_METERS = {1: 3.0, 2: 5.0, 3: 7.0, 4: 9.0, 5: 12.0}

# CityScapes-style tags kept from the semantic segmentation stream.
SEMANTIC_TAGS = {4: "pedestrian", 10: "vehicle", 12: "traffic_sign",
                 18: "traffic_light", 23: "rider"}
WALKER_TARGET_COUNT = 6
RIDER_TARGET_COUNT = 2
SENSOR_NAMES = ("gnss", "imu", "lidar", "radar", "segmentation", "depth", "obstacle")

# Preset keys map to the exact carla.WeatherParameters attribute names;
# 0.9.16 ships no Fog preset, so fog_noon is built from raw parameters.
CARLA_WEATHER_ATTRIBUTES = {"clear_noon": "ClearNoon", "clear_sunset": "ClearSunset",
                            "clear_night": "ClearNight", "cloudy_noon": "CloudyNoon",
                            "rain_noon": "MidRainyNoon", "hard_rain_noon": "HardRainNoon"}


def weather_preset_parameters(carla_module, preset: str):
    attribute = CARLA_WEATHER_ATTRIBUTES.get(preset)
    if attribute is not None:
        return getattr(carla_module.WeatherParameters, attribute, None)
    if preset == "fog_noon":
        return carla_module.WeatherParameters(
            cloudiness=70.0, precipitation=0.0, wind_intensity=10.0,
            fog_density=80.0, fog_distance=15.0, wetness=0.0,
            sun_azimuth_angle=0.0, sun_altitude_angle=45.0)
    return None
