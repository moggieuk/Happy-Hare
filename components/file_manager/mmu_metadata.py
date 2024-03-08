import metadata
import runpy

if __name__ == "__main__":
    runpy.run_module('metadata', run_name="__main__")
    metadata.logger.info("THIS WAS THE MMU_METADATA")

