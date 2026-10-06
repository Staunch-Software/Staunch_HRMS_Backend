from typing import Optional

from pydantic import BaseModel, field_validator


class DepartmentBase(BaseModel):
    name: str
    code: str
    description: Optional[str] = None
    status: str = "Active"

    @field_validator("code")
    @classmethod
    def uppercase_code(cls, v: str) -> str:
        return v.strip().upper()


class DepartmentCreate(DepartmentBase):
    pass


class DepartmentUpdate(DepartmentBase):
    pass


class DepartmentRead(DepartmentBase):
    id: int

    model_config = {"from_attributes": True}


class DesignationBase(BaseModel):
    name: str
    department_id: Optional[int] = None
    status: str = "Active"


class DesignationCreate(DesignationBase):
    pass


class DesignationUpdate(DesignationBase):
    pass


class DesignationRead(DesignationBase):
    id: int

    model_config = {"from_attributes": True}


class WorkLocationBase(BaseModel):
    name: str
    state_code: str
    status: str = "Active"


class WorkLocationCreate(WorkLocationBase):
    pass


class WorkLocationUpdate(WorkLocationBase):
    pass


class WorkLocationRead(WorkLocationBase):
    id: int

    model_config = {"from_attributes": True}
