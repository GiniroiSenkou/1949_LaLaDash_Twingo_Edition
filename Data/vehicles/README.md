# Vehicle profiles

`default.json` holds the numbers that describe the reference vehicle (a Renault
Twingo III 0.9 TCe 90): warning bands, gear ratios, engine model for the fuel
estimate, tank size. To adapt LaLaDash to another car, copy it to
`Data/vehicles/<name>.json`, keep only the keys you want to change, and set

```ini
[Vehicle]
profile = <name>
```

in `Data/settings.ini`. Missing keys fall back to `default.json`. Profiles are
read once at start-up (restart to apply).
