#!/bin/bash

#!/bin/sh

process_pairs() {
    while read -r from to; do
        echo "/n/nConverting ${from} to ${to}..."

        find . -name "*.md" -print | while read line
        do
            echo "Processing ${line}..."
            cat $line | sed -e "s%$from%$to%g" > /tmp/f.out && mv /tmp/f.out "$line"
        done
    done
}

cat <<EOF | process_pairs
doc/toolchange/ doc/toolchage_movement/
doc/slicer/ doc/slicer_setup/
EOF
#doc/c5f015.png doc/resources/c5f015.png
#doc/1589F0.png doc/resources/1589F0.png
#EOF

#find . -name "*.md" -print | while read line
#do
#    echo "Processing ${line}..."
#    cat "$line" | sed -E "s%/doc/([^/]*\.(png|jpg))%/doc/resources/\1%g" > /tmp/f.out && mv /tmp/f.out "$line"
#done

