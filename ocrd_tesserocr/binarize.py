from __future__ import absolute_import

import os.path
from tesserocr import (
    PyTessBaseAPI,
    PSM, RIL
)

from ocrd_utils import (
    getLogger, concat_padded,
    MIMETYPE_PAGE
)
from ocrd_modelfactory import page_from_file
from ocrd_models.ocrd_page import (
    MetadataItemType,
    LabelsType, LabelType,
    AlternativeImageType,
    TextRegionType,
    to_xml
)
from ocrd import Processor

from .config import TESSDATA_PREFIX, OCRD_TOOL
from .common import (
    image_from_page,
    image_from_region,
    image_from_line,
    save_image_file,
    membername
)

TOOL = 'ocrd-tesserocr-binarize'
LOG = getLogger('processor.TesserocrBinarize')
FILEGRP_IMG = 'OCR-D-IMG-BIN'

class TesserocrBinarize(Processor):

    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools'][TOOL]
        kwargs['version'] = OCRD_TOOL['version']
        super(TesserocrBinarize, self).__init__(*args, **kwargs)

    def process(self):
        """Performs binarization with Tesseract on the workspace.
        
        Open and deserialize PAGE input files and their respective images,
        then iterate over the element hierarchy down to the requested level.
        
        Set up Tesseract to recognize the segment image's layout, and get
        the binarized image. Create an image file, and reference it as
        AlternativeImage in the element and as file with a fileGrp USE
        equal `OCR-D-IMG-BIN` in the workspace.
        
        Produce a new output file by serialising the resulting hierarchy.
        """
        oplevel = self.parameter['operation_level']
        with PyTessBaseAPI(path=TESSDATA_PREFIX) as tessapi:
            for n, input_file in enumerate(self.input_files):
                file_id = input_file.ID.replace(self.input_file_grp, FILEGRP_IMG)
                page_id = input_file.pageId or input_file.ID
                LOG.info("INPUT FILE %i / %s", n, page_id)
                pcgts = page_from_file(self.workspace.download_file(input_file))
                metadata = pcgts.get_Metadata() # ensured by from_file()
                metadata.add_MetadataItem(
                    MetadataItemType(type_="processingStep",
                                     name=self.ocrd_tool['steps'][0],
                                     value=TOOL,
                                     # FIXME: externalRef is invalid by pagecontent.xsd, but ocrd does not reflect this
                                     # what we want here is `externalModel="ocrd-tool" externalId="parameters"`
                                     Labels=[LabelsType(#externalRef="parameters",
                                                        Label=[LabelType(type_=name,
                                                                         value=self.parameter[name])
                                                               for name in self.parameter.keys()])]))
                page = pcgts.get_Page()
                page_image = self.workspace.resolve_image_as_pil(page.imageFilename)
                LOG.info("Binarizing on '%s' level in page '%s'", oplevel, page_id)

                page_image, page_xywh = image_from_page(
                    self.workspace, page, page_image, page_id)
                if oplevel == 'page':
                    tessapi.SetPageSegMode(PSM.AUTO)
                    self._process_segment(tessapi, RIL.BLOCK, page, page_image, page_xywh,
                                          "page '%s'" % page_id, input_file.pageId,
                                          file_id)
                else:
                    regions = page.get_TextRegion() + page.get_TableRegion()
                    if not regions:
                        LOG.warning("Page '%s' contains no text regions", page_id)
                    for region in regions:
                        region_image, region_xywh = image_from_region(
                            self.workspace, region, page_image, page_xywh)
                        if oplevel == 'region':
                            tessapi.SetPageSegMode(PSM.SINGLE_BLOCK)
                            self._process_segment(tessapi, RIL.BLOCK, region, region_image, region_xywh,
                                                  "region '%s'" % region.id, input_file.pageId,
                                                  file_id + '_' + region.id)
                        elif isinstance(region, TextRegionType):
                            lines = region.get_TextLine()
                            if not lines:
                                LOG.warning("Page '%s' region '%s' contains no text lines",
                                            page_id, region.id)
                            for line in lines:
                                line_image, line_xywh = image_from_line(
                                    self.workspace, line, region_image, region_xywh)
                                tessapi.SetPageSegMode(PSM.SINGLE_LINE)
                                self._process_segment(tessapi, RIL.TEXTLINE, line, line_image, line_xywh,
                                                      "line '%s'" % line.id, input_file.pageId,
                                                      file_id + '_' + region.id + '_' + line.id)

                # Use input_file's basename for the new file -
                # this way the files retain the same basenames:
                file_id = input_file.ID.replace(self.input_file_grp, self.output_file_grp)
                if file_id == input_file.ID:
                    file_id = concat_padded(self.output_file_grp, n)
                self.workspace.add_file(
                    ID=file_id,
                    file_grp=self.output_file_grp,
                    mimetype=MIMETYPE_PAGE,
                    local_filename=os.path.join(self.output_file_grp,
                                                file_id + '.xml'),
                    content=to_xml(pcgts))

    def _process_segment(self, tessapi, ril, segment, image, xywh, where, page_id, file_id):
        tessapi.SetImage(image)
        image_bin = None
        layout = tessapi.AnalyseLayout()
        if layout:
            image_bin = layout.GetBinaryImage(ril)
        if not image_bin:
            LOG.error('Cannot binarize %s', where)
            return
        # update METS (add the image file):
        file_path = save_image_file(self.workspace, image_bin,
                                    file_id,
                                    page_id=page_id,
                                    file_grp=FILEGRP_IMG)
        # update PAGE (reference the image file):
        segment.add_AlternativeImage(AlternativeImageType(
            filename=file_path, comments="binarized"))
