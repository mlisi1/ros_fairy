# Standards and references

ROS F.A.I.R.y doesn't invent a data format. It builds on established
research-data standards, so that a saved mission can be understood by tools
and people who have never heard of ROS.

## FAIR

The FAIR Guiding Principles say that research data should be **F**indable,
**A**ccessible, **I**nteroperable and **R**eusable, by people and by machines.
They are the reason for the project's name.

| Principle | How ROS F.A.I.R.y applies it |
| --- | --- |
| Findable | Every mission gets a unique ID, a goal, a place, an operator and a date, and is listed in a searchable index ([`list`](commands.md#list)). |
| Accessible | A mission is a plain folder, or one checksummed `.zip`/`.tar` ([`export`](commands.md#export)), readable without ROS F.A.I.R.y. |
| Interoperable | Metadata is RO-Crate JSON-LD using schema.org and SOSA/SSN terms. Recordings stay standard rosbag2 (MCAP or sqlite3). |
| Reusable | The full context is recorded: ROS graph, parameters, URDF, software, hardware, udev rules, the build that saved it, recording health and an optional data license ([what is captured](what-is-captured.md)). |

> Wilkinson, M. D., Dumontier, M., Aalbersberg, I. J., et al. (2016).
> The FAIR Guiding Principles for scientific data management and stewardship.
> *Scientific Data*, 3, 160018.
> [doi:10.1038/sdata.2016.18](https://doi.org/10.1038/sdata.2016.18)

## RO-Crate

RO-Crate is a community standard for packaging research data with its
metadata: a folder of files plus one `ro-crate-metadata.json` that describes
them in JSON-LD, using [schema.org](https://schema.org). Every mission
ROS F.A.I.R.y saves is an RO-Crate 1.1 ([archive format](archive-format.md#the-ro-crate)).

> Soiland-Reyes, S., Sefton, P., Crosas, M., et al. (2022).
> Packaging research artefacts with RO-Crate.
> *Data Science*, 5(2), 97–138.
> [doi:10.3233/DS-210053](https://doi.org/10.3233/DS-210053)

> Sefton, P., Ó Carragáin, E., Soiland-Reyes, S., et al. (2020).
> *RO-Crate Metadata Specification 1.1*.
> [doi:10.5281/zenodo.4031327](https://doi.org/10.5281/zenodo.4031327) ·
> [w3id.org/ro/crate/1.1](https://w3id.org/ro/crate/1.1)

## SOSA/SSN

The robot and its sensors are described in the RO-Crate with SOSA (Sensor,
Observation, Sample and Actuator), the core of the W3C Semantic Sensor Network
ontology: the robot is a `sosa:Platform` that `sosa:hosts` each declared
`sosa:Sensor`.

> Haller, A., Janowicz, K., Cox, S., Le Phuoc, D., Taylor, K., Lefrançois, M.
> (eds.) (2017). *Semantic Sensor Network Ontology*. W3C Recommendation.
> [w3.org/TR/vocab-ssn](https://www.w3.org/TR/vocab-ssn/)

> Janowicz, K., Haller, A., Cox, S. J. D., Le Phuoc, D., & Lefrançois, M. (2019).
> SOSA: A lightweight ontology for sensors, observations, samples, and actuators.
> *Journal of Web Semantics*, 56, 1–10.
> [doi:10.1016/j.websem.2018.06.003](https://doi.org/10.1016/j.websem.2018.06.003)

## Recordings

Recordings are kept exactly as [rosbag2](https://github.com/ros2/rosbag2) wrote
them, in [MCAP](https://mcap.dev) or sqlite3 storage, so any ROS 2 tool can
play them back.
