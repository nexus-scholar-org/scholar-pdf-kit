from typing import Optional
from pydantic import BaseModel, Field, ConfigDict

class OALocation(BaseModel):
    """Represents a specific Open Access location returned by Unpaywall."""
    model_config = ConfigDict(extra="ignore")
    
    is_best: bool
    endpoint_id: str | None = None
    evidence: str | None = None
    host_type: str | None = None
    is_oa: bool = True
    license: str | None = None
    pmh_id: str | None = None
    repository_institution: str | None = None
    updated: str | None = None
    url: str
    url_for_landing_page: str | None = None
    url_for_pdf: str | None = None
    version: str | None = None


class OAResult(BaseModel):
    """The root response object for a DOI from Unpaywall."""
    model_config = ConfigDict(extra="ignore")
    
    doi: str
    is_oa: bool
    data_standard: int
    title: str | None = None
    year: int | None = None
    journal_is_oa: bool = False
    journal_is_in_doaj: bool = False
    journal_name: str | None = None
    publisher: str | None = None
    
    best_oa_location: Optional[OALocation] = None
    oa_locations: list[OALocation] = Field(default_factory=list)

    @property
    def best_pdf_url(self) -> Optional[str]:
        """Convenience method to get the best PDF URL."""
        if self.best_oa_location and self.best_oa_location.url_for_pdf:
            return self.best_oa_location.url_for_pdf
            
        # Fallback to checking other locations
        for loc in self.oa_locations:
            if loc.url_for_pdf:
                return loc.url_for_pdf
        return None
