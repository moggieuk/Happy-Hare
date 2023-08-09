import re, textwrap, logging, os
from itertools import takewhile, dropwhile

class MmuFileProcessor:
    # Matches on T<tool> and MMU_CHANGE_TOOL TOOL=<tool>
    TOOL_DISCOVERY_REGEX = r'((^MMU_CHANGE_TOOL.*?TOOL=)|(^T))(?P<tool>\d{1,2})'
    METADATA_START_STRING = '[mmu_file_stats]'
    METADATA_END_STRING = '[end_mmu_file_stats]'

    def __init__(self, _config = {}):
        self._logger = None

    def set_logger(self, log):
        self._logger = log

    def write_mmu_metadata(self, file_path):
        self._log('Writing MMU metadata to file: ' + file_path)
        tool_use_frequency = self._enumerate_used_tools(file_path)
        tools_used = self._determine_unique_tools(tool_use_frequency)

        with open(file_path, 'a') as f:
            f.write(self._compose_tool_usage_string(tool_use_frequency, tools_used))

        return {
            'tools_used': tools_used,
            'tool_use_frequency': tool_use_frequency
        }

    def read_mmu_metadata(self, file_path, write_if_missing=True):
        self._log('Reading MMU metadata from file: ' + file_path)
        metadata = self._extract_metadata_from_file(file_path)

        if not metadata and write_if_missing:
            self._log('No metadata found - generating and writing to file')
            return self.write_mmu_metadata(file_path)

        return metadata

    def _log(self, message):
        if self._logger:
            self._logger('MMU File Processor: ' + message)

    def _enumerate_used_tools(self, file_path):
        regex = re.compile(self.TOOL_DISCOVERY_REGEX, re.IGNORECASE)
        tools_used = {}

        with open(file_path, 'r') as f:
            for line in f:
                match = regex.match(line)
                if match:
                    tool = match.group('tool')
                    if tool not in tools_used:
                        tools_used[tool] = 0
                    tools_used[tool] += 1

        return tools_used

    def _determine_unique_tools(self, tool_use_frequency):
        return sorted(list(map(lambda tool: int(tool), tool_use_frequency.keys())))
    
    def _compose_tool_usage_string(self, tool_use_frequency, tools_used):
        tool_use_string = ','.join(map(str, tools_used))
        tool_use_frequency_string = ','.join(map(
            lambda tool: f'[{tool},{tool_use_frequency[str(tool)]}]',
            tools_used
        ))

        return textwrap.dedent(f'''
            ; {self.METADATA_START_STRING}
            ; MMU_TOOLS_USED={tool_use_string}
            ; MMU_TOOL_USE_FREQUENCY=[{tool_use_frequency_string}]
            ;
            ; (\_/)
            ; ( *,*)
            ; (")_(") MMU Ready
            ;
            ; {self.METADATA_END_STRING}
        ''')

    def _extract_metadata_from_file(self, file_path):
        parsed_metadata = {}

        with(open(file_path, 'r')) as f:
            start = dropwhile(lambda line: self.METADATA_START_STRING not in line, f)
            configuration_lines = takewhile(lambda line: self.METADATA_END_STRING not in line, start)
            metadata = list(configuration_lines)

            for metadatum in metadata:
                if 'MMU_' in metadatum and '=' in metadatum:
                    key, value = metadatum.split('=')
                    formatted_key = key.partition('MMU_')[2].strip().lower()
                    parsed_metadata[formatted_key] = self._rehydrate_value_from_key(formatted_key, value.strip())

        return parsed_metadata

    # NOTE: this hardcoding isn't ideal, but is likely good enough since the metadata is known ahead 
    # # of time and the list of keys is small
    def _rehydrate_value_from_key(self, formatted_key, value):
        if formatted_key == 'tools_used':
            if value == '':
                return []
    
            return list(map(lambda tool: int(tool), value.strip().split(',')))
        elif formatted_key == 'tool_use_frequency':
            if value == '[]' or value == '':
                return {}
            
            return dict(
                map(
                    lambda tool: (tool[0], int(tool[1])), 
                    map(
                        lambda tool: tool.strip('[]').split(','), 
                        value.strip().split('],[')
                    )
                )
            )

def load_config(config):
    return MmuFileProcessor(config)
