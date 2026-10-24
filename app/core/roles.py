from enum import StrEnum


class Role(StrEnum):
    STAFF = "staff"
    HOD = "hod"
    HR = "hr"
    DIRECTOR = "director"
    SUPERADMIN = "superadmin"


# Roles that can see hospital-wide records.
GLOBAL_ROLES = (Role.HR, Role.DIRECTOR, Role.SUPERADMIN)
